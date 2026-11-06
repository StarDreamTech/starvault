//! Local, extractive milestones. The archive remains the source of truth.
use super::{body, context_prefix, message_cards, Session};
use pulldown_cmark::{Event, Parser, Tag, TagEnd};
use serde::{Deserialize, Serialize};
use std::{
    collections::{hash_map::DefaultHasher, BTreeMap},
    fs::{self, OpenOptions},
    hash::{Hash, Hasher},
    io::Write,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

const VERSION: u32 = 3;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub(super) struct Node {
    pub message_index: usize,
    pub timestamp: String,
    pub kind: String,
    pub title: String,
    pub request: String,
    pub response: String,
}

#[derive(Default)]
pub(super) struct ScanStats {
    pub scanned: usize,
    pub reused: usize,
}

fn excerpt(text: &str, limit: usize) -> String {
    let compact = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut result: String = compact.chars().take(limit).collect();
    if compact.chars().count() > limit {
        result.push('…');
    }
    result
}

fn prose(markdown: &str) -> String {
    let mut text = markdown.trim();
    while let Some((rest, _)) = context_prefix(text) {
        text = rest;
    }
    let (text, _) = super::separate_attachment_context(text);
    // Exported image timestamps describe attachments, not the user's request.
    let text = text
        .lines()
        .filter(|line| {
            !line
                .strip_prefix("### 图片 · ")
                .is_some_and(|date| chrono::DateTime::parse_from_rfc3339(date.trim()).is_ok())
        })
        .collect::<Vec<_>>()
        .join("\n");
    let mut result = String::new();
    let mut skip = 0;
    for event in Parser::new(&text) {
        match event {
            Event::Start(tag) => {
                if skip > 0
                    || matches!(
                        tag,
                        Tag::CodeBlock(_) | Tag::Image { .. } | Tag::BlockQuote(_)
                    )
                {
                    skip += 1;
                }
            }
            Event::End(_) if skip > 0 => skip -= 1,
            _ if skip > 0 => {}
            Event::Text(value) | Event::Code(value) => result.push_str(&value),
            Event::SoftBreak | Event::HardBreak | Event::Rule => result.push('\n'),
            Event::End(
                TagEnd::Paragraph | TagEnd::Heading(_) | TagEnd::Item | TagEnd::TableRow,
            ) => {
                result.push('\n');
            }
            _ => {}
        }
    }
    result.trim().to_owned()
}

fn contains(text: &str, terms: &[&str]) -> bool {
    terms.iter().any(|term| {
        if term.is_ascii() {
            // Do not mistake "debug" for a reported "bug", or "address" for "add".
            text.match_indices(term).any(|(at, _)| {
                let before = text[..at].chars().next_back();
                let after = text[at + term.len()..].chars().next();
                !before.is_some_and(|c| c.is_ascii_alphanumeric())
                    && !after.is_some_and(|c| c.is_ascii_alphanumeric())
            })
        } else {
            text.contains(term)
        }
    })
}

fn completed(text: &str) -> bool {
    !text.ends_with(['?', '？'])
        && !contains(
            text,
            &[
                "还没",
                "尚未",
                "未完成",
                "未修复",
                "没有",
                "并未",
                "无法",
                "不能",
                "失败",
                "不通过",
                "未通过",
                "计划",
                "准备",
                "将会",
                "我会",
                "将要",
                "预计",
                "打算",
                "待完成",
                "待修复",
                "not",
                "isn't",
                "wasn't",
                "haven't",
                "hasn't",
                "pending",
                "plan to",
                "will",
                "going to",
                "would",
                "should",
                "could",
                "if",
            ],
        )
}

// ponytail: deterministic phrase matching keeps this offline; use an explicit optional
// summarizer if semantic recall is needed. Titles and excerpts always quote the dialogue.
fn classify(text: &str, user: bool) -> Option<(&'static str, &str)> {
    let sentences: Vec<_> = text
        .split_inclusive(['。', '！', '？', '?', '!', '\n', ';', '；', '.'])
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect();
    let rules: &[(&str, &[&str])] = &[
        (
            "修复",
            &[
                "已修复",
                "已经修复",
                "修复了",
                "已解决",
                "已经解决",
                "解决了",
                "已恢复",
                "已纠正",
                "fixed",
                "resolved",
                "repaired",
            ],
        ),
        (
            "成果",
            &[
                "已完成",
                "已经完成",
                "完成了",
                "已实现",
                "实现了",
                "已加入",
                "已新增",
                "已添加",
                "已接入",
                "已上线",
                "已发布",
                "已交付",
                "已生成",
                "已保存",
                "已导出",
                "已打包",
                "已通过",
                "测试通过",
                "全部通过",
                "验证通过",
                "已提交",
                "已安装",
                "已更新",
                "已创建",
                "implemented",
                "completed",
                "shipped",
                "published",
                "released",
                "tests passed",
                "all tests pass",
                "saved",
                "created",
                "added",
                "updated",
            ],
        ),
        (
            "决定",
            &[
                "决定",
                "确定采用",
                "就用",
                "选用",
                "按这个方案",
                "按方案",
                "改用",
                "确认方案",
                "同意这个",
                "选择方案",
                "最终选择",
                "选方案",
                "采用",
                "decided to",
                "let's use",
                "use option",
                "go with",
                "chosen",
                "choose",
            ],
        ),
        (
            "问题",
            &[
                "报错",
                "崩溃",
                "失败",
                "异常",
                "卡住",
                "无法",
                "不显示",
                "不生效",
                "错误",
                "缺失",
                "没反应",
                "闪退",
                "bug",
                "error",
                "crash",
                "fails",
                "failed",
                "broken",
                "doesn't work",
                "not working",
                "missing",
            ],
        ),
        (
            "需求",
            &[
                "加个",
                "新增",
                "增加",
                "添加",
                "实现",
                "希望",
                "需要",
                "帮我",
                "优化",
                "调整",
                "改成",
                "改为",
                "支持",
                "设计",
                "做一个",
                "做个",
                "制作",
                "修改",
                "重构",
                "删除",
                "移除",
                "修复",
                "build",
                "implement",
                "add",
                "create",
                "remove",
                "change",
                "i want",
                "we need",
                "can you",
                "could you",
                "fix",
            ],
        ),
    ];
    for &(kind, terms) in rules {
        if kind == "需求" && !user {
            continue;
        }
        for &sentence in &sentences {
            let mut lower = sentence.to_lowercase();
            if kind == "问题" {
                for absent in [
                    "没有报错",
                    "没有错误",
                    "没有异常",
                    "没有问题",
                    "无错误",
                    "无异常",
                    "未发现问题",
                    "未发现错误",
                    "没有发现异常",
                    "不再报错",
                    "不再崩溃",
                    "no errors",
                    "no error",
                    "no issues",
                    "no issue",
                    "no bugs",
                    "no bug",
                    "no crashes",
                    "not broken",
                    "without errors",
                ] {
                    lower = lower.replace(absent, "");
                }
            }
            if kind == "决定"
                && (sentence.ends_with(['?', '？'])
                    || contains(
                        &lower,
                        &[
                            "稍后",
                            "再采用",
                            "考虑",
                            "建议",
                            "可以采用",
                            "可能",
                            "计划采用",
                            "准备采用",
                            "打算",
                            "暂不",
                            "不采用",
                            "尚未决定",
                            "还没决定",
                            "是否",
                            "如果",
                            "later",
                            "consider",
                            "might",
                            "could",
                            "would",
                            "maybe",
                            "recommend",
                            "propose",
                            "will decide",
                            "not decided",
                        ],
                    ))
            {
                continue;
            }
            if contains(&lower, terms) && (!(kind == "成果" || kind == "修复") || completed(&lower))
            {
                return Some((kind, sentence));
            }
        }
    }
    None
}

pub(super) fn extract(markdown: &str) -> Vec<Node> {
    let mut nodes: Vec<Node> = Vec::new();
    let mut request = String::new();
    let mut result = None;
    let mut request_node: Option<usize> = None;
    for (message_index, card) in message_cards(body(markdown)).into_iter().enumerate() {
        if card.context.is_some() || !matches!(card.role, "用户" | "助手") {
            continue;
        }
        let text = prose(&card.markdown);
        let user = card.role == "用户";
        if user {
            if let Some(node) = result.take() {
                nodes.push(node);
            }
            request = excerpt(&text, 280);
            request_node = None;
        }
        if text.is_empty() {
            continue;
        }
        let classified = classify(&text, user);
        let response = if user {
            String::new()
        } else {
            let at = classified
                .and_then(|(_, sentence)| text.find(sentence))
                .unwrap_or(0);
            excerpt(&text[at..], 360)
        };
        if !user {
            if let Some(index) = request_node {
                nodes[index].response = response.clone();
            }
        }
        let Some((kind, sentence)) = classified else {
            continue;
        };
        let node = Node {
            message_index,
            timestamp: card.timestamp.trim().to_owned(),
            kind: kind.to_owned(),
            title: excerpt(sentence, 72),
            request: request.clone(),
            response,
        };
        if user {
            request_node = Some(nodes.len());
            nodes.push(node);
        } else {
            // One substantive report per user turn; later reports replace progress chatter.
            result = Some(node);
        }
    }
    if let Some(node) = result {
        nodes.push(node);
    }
    nodes
}

#[derive(Deserialize, Serialize)]
struct CachedSession {
    fingerprint: u64,
    nodes: Vec<Node>,
}

#[derive(Default, Deserialize, Serialize)]
struct Cache {
    version: u32,
    archive: PathBuf,
    sessions: BTreeMap<String, CachedSession>,
}

fn hash(value: impl Hash) -> u64 {
    let mut hasher = DefaultHasher::new();
    value.hash(&mut hasher);
    hasher.finish()
}

fn cache_path(directory: &Path, archive: &Path) -> PathBuf {
    directory.join(format!("{:016x}.json", hash(archive)))
}

fn save_cache(path: &Path, cache: &Cache) -> Result<(), String> {
    let bytes = serde_json::to_vec(cache).map_err(|e| e.to_string())?;
    fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = path.with_extension(format!("{}-{unique}.tmp", std::process::id()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let result = (|| {
        let mut file = options.open(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.map_err(|e: std::io::Error| e.to_string())
}

fn refresh_at(
    root: &Path,
    sessions: &[Session],
    directory: &Path,
) -> (Vec<Vec<Node>>, ScanStats, Vec<String>) {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let path = cache_path(directory, &root);
    let mut warnings = Vec::new();
    let mut old = match fs::read(&path) {
        Ok(bytes) => match serde_json::from_slice::<Cache>(&bytes) {
            Ok(cache) if cache.version == VERSION && cache.archive == root => cache,
            Ok(_) => Cache::default(),
            Err(_) => {
                warnings.push("时间线缓存损坏，已从对话重新整理".to_owned());
                Cache::default()
            }
        },
        Err(e) => {
            if e.kind() != std::io::ErrorKind::NotFound {
                warnings.push(format!("无法读取时间线缓存，已重新整理：{e}"));
            }
            Cache::default()
        }
    };
    let mut changed = old.version != VERSION || old.sessions.len() != sessions.len();
    let mut cache = Cache {
        version: VERSION,
        archive: root,
        sessions: BTreeMap::new(),
    };
    let mut stats = ScanStats::default();
    let mut output = Vec::with_capacity(sessions.len());
    for session in sessions {
        let key = format!("{}\0{}", session.branch, session.meta.id);
        let fingerprint = hash((
            VERSION,
            &session.markdown,
            &session.meta.started_at,
            &session.meta.updated_at,
        ));
        let entry = match old.sessions.remove(&key) {
            Some(entry) if entry.fingerprint == fingerprint => {
                stats.reused += 1;
                entry
            }
            _ => {
                stats.scanned += 1;
                changed = true;
                CachedSession {
                    fingerprint,
                    nodes: extract(&session.markdown),
                }
            }
        };
        output.push(entry.nodes.clone());
        cache.sessions.insert(key, entry);
    }
    if changed {
        if let Err(error) = save_cache(&path, &cache) {
            warnings.push(format!("时间线已整理，但缓存未保存：{error}"));
        }
    }
    (output, stats, warnings)
}

pub(super) fn refresh(
    root: &Path,
    sessions: &[Session],
) -> (Vec<Vec<Node>>, ScanStats, Vec<String>) {
    refresh_at(root, sessions, &super::app_home().join("timeline"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_evidence_and_links_actual_message_instead_of_context_or_code() {
        let source = "# example\n\n## 用户 · 2026-10-01T01:00:00Z\n<environment_context>cwd</environment_context>\n\n## 用户 · 2026-10-01T01:01:00Z\n<environment_context>cwd</environment_context>\n请添加项目时间线。\n```text\n## 助手 · 2026-10-01T01:02:00Z\n已完成假的内容。\n```\n\n## 助手 · 2026-10-01T01:03:00Z\n我会完成项目时间线。\n\n## 助手 · 2026-10-01T01:04:00Z\n已实现项目时间线。测试通过。\n\n## 用户 · \n继续\n\n## 助手 · \n已修复筛选错误。\n";
        let nodes = extract(source);
        assert_eq!(
            nodes.iter().map(|n| n.kind.as_str()).collect::<Vec<_>>(),
            ["需求", "成果", "修复"]
        );
        assert_eq!(
            nodes.iter().map(|n| n.message_index).collect::<Vec<_>>(),
            [1, 3, 5]
        );
        assert_eq!(nodes[1].timestamp, "2026-10-01T01:04:00Z");
        assert_eq!(nodes[2].timestamp, "");
        assert!(!nodes
            .iter()
            .any(|n| n.title.contains("假的") || n.title.contains("cwd")));
        assert_eq!(nodes[2].request, "继续");
        assert_eq!(nodes[0].response, "已实现项目时间线。测试通过。");
        assert_eq!(nodes[0].response, nodes[1].response);
    }

    #[test]
    fn plans_negations_and_casual_chat_do_not_claim_completion() {
        for statement in [
            "我会修复这个错误。",
            "没有修复，也没有完成。",
            "计划已完成后再发布。",
            "I will have fixed this tomorrow.",
            "This is not completed.",
            "已完成了吗？",
        ] {
            let nodes = extract(&format!("## 助手 · \n{statement}"));
            assert!(
                !nodes
                    .iter()
                    .any(|n| matches!(n.kind.as_str(), "修复" | "成果")),
                "{statement}"
            );
        }
        assert!(extract("## 用户 · \n谢谢\n## 助手 · \n不客气\n## 用户 · \n继续").is_empty());
        assert!(classify("Let's debug the address parser.", false).is_none());
        assert_eq!(classify("就用本地缓存方案。", true).unwrap().0, "决定");
        assert_eq!(classify("点击按钮后会崩溃。", true).unwrap().0, "问题");
        for statement in [
            "没有报错。",
            "未发现错误。",
            "没有发现异常。",
            "No errors found.",
            "决定稍后再采用。",
        ] {
            assert!(classify(statement, false).is_none(), "{statement}");
        }
        assert_eq!(
            classify("没有报错，但按钮不显示。", false).unwrap().0,
            "问题"
        );
    }

    #[test]
    fn image_and_code_only_requests_start_a_new_turn() {
        assert_eq!(
            prose("请添加搜索。\n### 图片 · 2026-10-29T03:00:00Z\n![截图](../../../images/a.png)"),
            "请添加搜索。"
        );
        for request in [
            "![截图](../../../images/a.png)",
            "```rust\nfn main() {}\n```",
        ] {
            let nodes = extract(&format!("## 用户 · \n请添加搜索。\n## 助手 · \n已实现搜索。\n## 用户 · \n{request}\n## 助手 · \n已修复截图中的问题。"));
            assert_eq!(nodes.len(), 3);
            assert_eq!(nodes[0].response, "已实现搜索。");
            assert!(nodes[2].request.is_empty());
            assert_eq!(nodes[2].message_index, 3);
        }
    }

    #[test]
    fn cache_reuses_replaces_isolates_and_recovers() {
        let dir = std::env::temp_dir().join(format!(
            "vault-timeline-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let root = dir.join("archive-a");
        let other = dir.join("archive-b");
        let cache_dir = dir.join("cache");
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&other).unwrap();
        let mut sessions = vec![Session {
            meta: super::super::SessionMeta {
                id: "same-id".into(),
                ..Default::default()
            },
            display_title: String::new(),
            branch: "laptop-codex".into(),
            device: String::new(),
            path: root.join("conversation.md"),
            markdown: "## 用户 · \n请添加搜索。".into(),
            search: String::new(),
        }];
        let (first, stats, warnings) = refresh_at(&root, &sessions, &cache_dir);
        assert!(warnings.is_empty());
        assert_eq!((stats.scanned, stats.reused), (1, 0));
        let cache_file = cache_path(&cache_dir, &root.canonicalize().unwrap());
        let modified = fs::metadata(&cache_file).unwrap().modified().unwrap();
        let (second, stats, _) = refresh_at(&root, &sessions, &cache_dir);
        assert_eq!(first, second);
        assert_eq!((stats.scanned, stats.reused), (0, 1));
        assert_eq!(
            modified,
            fs::metadata(&cache_file).unwrap().modified().unwrap()
        );
        sessions[0].markdown.push_str("\n## 助手 · \n已实现搜索。");
        let (third, stats, _) = refresh_at(&root, &sessions, &cache_dir);
        assert_eq!((stats.scanned, stats.reused), (1, 0));
        assert_eq!(third[0].len(), 2);
        assert_eq!(third[0][0].message_index, first[0][0].message_index);
        assert_eq!(third[0][0].title, first[0][0].title);
        assert_eq!(third[0][0].response, "已实现搜索。");
        let (fourth, stats, _) = refresh_at(&other, &sessions, &cache_dir);
        assert_eq!(third, fourth);
        assert_eq!((stats.scanned, stats.reused), (1, 0));
        sessions[0].branch = "desktop-codex".into();
        let (_, stats, _) = refresh_at(&root, &sessions, &cache_dir);
        assert_eq!((stats.scanned, stats.reused), (1, 0));
        let mut outdated: Cache = serde_json::from_slice(&fs::read(&cache_file).unwrap()).unwrap();
        outdated.version = VERSION - 1;
        save_cache(&cache_file, &outdated).unwrap();
        let (upgraded, stats, _) = refresh_at(&root, &sessions, &cache_dir);
        assert_eq!(upgraded, third);
        assert_eq!((stats.scanned, stats.reused), (1, 0));
        fs::write(&cache_file, "{broken").unwrap();
        let (rebuilt, stats, warnings) = refresh_at(&root, &sessions, &cache_dir);
        assert_eq!(third, rebuilt);
        assert_eq!(stats.scanned, 1);
        assert_eq!(warnings.len(), 1);
        fs::remove_dir_all(dir).unwrap();
    }
}
