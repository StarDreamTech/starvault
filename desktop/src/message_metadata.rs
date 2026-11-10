use super::MessageCard;
use serde_json::Value;
use std::{collections::BTreeMap, fs::File, io::Read, path::Path, sync::LazyLock};

/// Read only the selected conversation's local sidecar. Markdown remains the
/// source of message order, so missing or older records cannot shift jump targets.
pub(super) fn apply(cards: &mut [MessageCard], manifest: &Path) {
    let Some(directory) = manifest.parent() else {
        return;
    };
    let Ok(directory) = directory.canonicalize() else {
        return;
    };
    let Ok(path) = directory.join("dialogue.jsonl").canonicalize() else {
        return;
    };
    if !path.starts_with(&directory) {
        return;
    }
    let Ok(file) = File::open(path) else { return };
    // ponytail: enrichment is optional and bounded; large archives still read
    // through Markdown without loading another entire transcript on the UI thread.
    const LIMIT: u64 = 16 * 1024 * 1024;
    if file.metadata().map_or(true, |meta| meta.len() >= LIMIT) {
        return;
    }
    let mut data = String::new();
    if file.take(LIMIT).read_to_string(&mut data).is_err() || data.len() as u64 >= LIMIT {
        return;
    }
    let mut positions = BTreeMap::<(String, String), Vec<usize>>::new();
    for (index, card) in cards.iter().enumerate() {
        positions
            .entry((card.role.into(), card.timestamp.clone()))
            .or_default()
            .push(index);
    }
    // split on actual JSONL newlines, not Unicode line separators inside strings.
    for line in data.split('\n') {
        let Ok(row) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if row.get("isMeta").and_then(Value::as_bool) != Some(true)
            || row.get("turnOrigin").and_then(Value::as_str) == Some("human")
            || row.pointer("/origin/kind").and_then(Value::as_str) == Some("human")
        {
            continue;
        }
        let Some(message) = row.get("message").or_else(|| row.get("payload")) else {
            continue;
        };
        if message.get("role").and_then(Value::as_str) != Some("user") {
            continue;
        }
        let timestamp = row
            .get("timestamp")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let Some(indices) = positions.get(&("用户".into(), timestamp.into())) else {
            continue;
        };
        let text = match message.get("content") {
            Some(Value::String(text)) => text.clone(),
            Some(Value::Array(blocks)) => blocks
                .iter()
                .filter_map(|block| {
                    matches!(
                        block.get("type").and_then(Value::as_str),
                        Some("text" | "input_text" | "output_text")
                    )
                    .then(|| block.get("text").and_then(Value::as_str))
                    .flatten()
                })
                .collect::<Vec<_>>()
                .join("\n\n"),
            _ => continue,
        };
        for &index in indices {
            let card = &mut cards[index];
            // No positional guessing: exports may omit tool blocks or transform images.
            let Some(suffix) = card.original.trim().strip_prefix(text.trim()) else {
                continue;
            };
            if !suffix.trim().is_empty() && !exported_images_only(suffix) {
                continue;
            }
            // Exporters append tool screenshots to the preceding text message.
            // Keep them attached, but never mistake the whole Skill for a human request.
            card.markdown = suffix.to_owned();
            card.context_image_count = suffix
                .lines()
                .filter(|line| line.starts_with("![图片]("))
                .count();
            card.context = Some(
                if text
                    .trim_start()
                    .starts_with("Base directory for this skill:")
                {
                    "Skill 上下文"
                } else if text.trim_start().starts_with("<relay ") {
                    "Agent 协作"
                } else {
                    "会话上下文"
                },
            );
        }
    }
}

fn exported_images_only(text: &str) -> bool {
    static IMAGE: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(
            r"\A!\[图片\]\(\.\./\.\./\.\./images/[a-f0-9]{64}\.(?:png|jpe?g|gif|webp)\)\z",
        )
        .unwrap()
    });
    let mut images = 0;
    let mut group_images = 0;
    let mut has_heading = false;
    for line in text.lines().map(str::trim).filter(|line| !line.is_empty()) {
        if let Some(time) = line.strip_prefix("### 图片 ·") {
            if (has_heading && group_images == 0)
                || (!time.trim().is_empty()
                    && chrono::DateTime::parse_from_rfc3339(time.trim()).is_err())
            {
                return false;
            }
            has_heading = true;
            group_images = 0;
        } else if has_heading && IMAGE.is_match(line) {
            images += 1;
            group_images += 1;
        } else {
            return false;
        }
    }
    images > 0 && group_images > 0
}

/// Claude project notifications can carry a real human request. Only unwrap a
/// complete, single-message envelope; mixed text and unknown formats stay visible.
pub(super) fn human_request(text: &str) -> Option<String> {
    static WAKE: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(
        r#"(?s)\A\s*<wake\s[^>]*>\s*<project\s[^>]*>\s*<message\s(?P<attrs>[^>]*)>(?P<body>.*?)</message>\s*</project>\s*</wake>\s*\z"#
    ).unwrap()
    });
    static HUMAN: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(r#"(?:\A|\s)from\s*=\s*["']human["'](?:\s|\z)"#).unwrap()
    });
    let captures = WAKE.captures(text)?;
    if !HUMAN.is_match(&captures["attrs"]) || captures["body"].contains("</message>") {
        return None;
    }
    let body = captures["body"].trim();
    (!body.is_empty()).then(|| {
        body.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", "\"")
            .replace("&apos;", "'")
            .replace("&amp;", "&")
    })
}

pub(super) fn title(card: &MessageCard) -> String {
    match card.context {
        Some("Skill 上下文") => card
            .original
            .lines()
            .find_map(|line| line.strip_prefix("# "))
            .map(|heading| format!("Skill · {}", heading.chars().take(64).collect::<String>()))
            .unwrap_or_else(|| "Skill 上下文".into()),
        Some("Agent 协作") => "Agent 协作 · 协调任务".into(),
        Some(label) => label.into(),
        None => String::new(),
    }
}
