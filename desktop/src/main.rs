use chrono::{DateTime, Local, Utc};
use eframe::egui::{self, Color32, RichText};
use egui_commonmark::{CommonMarkCache, CommonMarkViewer};
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::{
    cell::RefCell,
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
    rc::Rc,
    sync::{mpsc, Arc, LazyLock, Mutex},
    thread,
    time::{Duration, Instant},
};

mod attachments_view;
mod brand;
mod filters_view;
mod message_metadata;
#[cfg(test)]
mod replies_view_tests;
#[cfg(test)]
mod settings_view_tests;
mod share;
mod timeline;
#[cfg(test)]
mod timeline_view_tests;

const ACCENT: Color32 = Color32::from_rgb(81, 94, 150);
const NAVY: Color32 = Color32::from_rgb(22, 32, 55);
const INK: Color32 = Color32::from_rgb(42, 44, 49);
const MUTED: Color32 = Color32::from_rgb(119, 123, 132);
const PAPER: Color32 = Color32::from_rgb(247, 247, 248);
const LINE: Color32 = Color32::from_rgb(230, 231, 235);
const SELECTED: Color32 = Color32::from_rgb(234, 237, 244);
const HOVER: Color32 = Color32::from_rgb(240, 241, 244);
const SESSION_ROW_HEIGHT: f32 = 50.0;
static IMAGE_LINK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"!\[([^\]]*)\]\(([^)\n]+)\)").unwrap());

#[derive(Clone, Default, Deserialize)]
#[serde(default)]
struct SessionMeta {
    id: String,
    title: String,
    tool: String,
    project: String,
    file: String,
    project_id: Option<String>,
    project_pinned: bool,
    project_order: Option<usize>,
    pinned: bool,
    updated_at: String,
    started_at: String,
    cwd: String,
    source: String,
    origin_kind: String,
    originator: String,
    models: Vec<String>,
    labels: Vec<String>,
    coverage: String,
    local_record: String,
    attachments: usize,
    operations: usize,
    archived: Option<bool>,
    messages: usize,
    assets: Vec<String>,
    unavailable_local_images: Vec<String>,
}

struct Session {
    meta: SessionMeta,
    display_title: String,
    branch: String,
    device: String,
    path: PathBuf,
    markdown: String,
    search: String,
}

#[derive(Default)]
struct Catalog {
    sessions: Vec<Session>,
    warnings: Vec<String>,
    timelines: Vec<Vec<timeline::Node>>,
    timeline_stats: timeline::ScanStats,
}

fn default_home() -> PathBuf {
    PathBuf::from(
        std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .unwrap_or_default(),
    )
}

fn app_home() -> PathBuf {
    if cfg!(target_os = "macos") {
        default_home().join("Library/Application Support/ChatHistoryVault")
    } else {
        default_home().join("ChatHistoryVault")
    }
}

#[derive(Default, Deserialize, Serialize)]
#[serde(default)]
struct Preferences {
    archive: String,
    images: String,
    font_size: f32,
    text_only: bool,
    projects_view: bool,
    timeline_view: bool,
    #[serde(default = "default_thumbnail_width")]
    thumbnail_width: u32,
    #[serde(default = "default_attachment_preview_width")]
    attachment_preview_width: u32,
}

fn default_attachment_preview_width() -> u32 {
    128
}

fn default_thumbnail_width() -> u32 {
    320
}

fn image_directory(root: &Path, configured: &str) -> PathBuf {
    if configured.is_empty() {
        root.join("images")
    } else if let Some(path) = configured.strip_prefix("~/") {
        default_home().join(path)
    } else {
        PathBuf::from(configured)
    }
}

// Read only requested images; decode URLs so copied folders can contain spaces,
// Chinese characters, or parentheses. In-flight reads can safely be discarded.
type ImageBytes = Option<Result<Arc<[u8]>, String>>;
#[derive(Default)]
struct LocalImageUrls {
    cache: Arc<Mutex<BTreeMap<String, ImageBytes>>>,
}
impl egui::load::BytesLoader for LocalImageUrls {
    fn id(&self) -> &str {
        "vault-local-images"
    }
    fn load(&self, ctx: &egui::Context, uri: &str) -> egui::load::BytesLoadResult {
        let Some(uri) = uri.strip_prefix("vault-") else {
            return Err(egui::load::LoadError::NotSupported);
        };
        let path = url::Url::parse(uri)
            .ok()
            .and_then(|u| u.to_file_path().ok())
            .ok_or_else(|| egui::load::LoadError::Loading("图片路径无效".into()))?;
        let mut cache = self.cache.lock().unwrap();
        match cache.get(uri) {
            Some(Some(Ok(bytes))) => {
                return Ok(egui::load::BytesPoll::Ready {
                    size: None,
                    bytes: egui::load::Bytes::Shared(bytes.clone()),
                    mime: None,
                })
            }
            Some(Some(Err(error))) => return Err(egui::load::LoadError::Loading(error.clone())),
            Some(None) => return Ok(egui::load::BytesPoll::Pending { size: None }),
            None => {}
        }
        cache.insert(uri.to_string(), None);
        let cache = self.cache.clone();
        let ctx = ctx.clone();
        let uri = uri.to_string();
        thread::spawn(move || {
            let result = fs::read(path).map(Arc::from).map_err(|e| e.to_string());
            // Switching conversations may have discarded this in-flight request.
            if let Some(entry) = cache.lock().unwrap().get_mut(&uri) {
                *entry = Some(result);
            }
            ctx.request_repaint();
        });
        Ok(egui::load::BytesPoll::Pending { size: None })
    }
    fn forget(&self, uri: &str) {
        self.cache
            .lock()
            .unwrap()
            .remove(uri.strip_prefix("vault-").unwrap_or(uri));
    }
    fn forget_all(&self) {
        self.cache.lock().unwrap().clear();
    }
    fn byte_size(&self) -> usize {
        self.cache
            .lock()
            .unwrap()
            .values()
            .filter_map(|v| v.as_ref()?.as_ref().ok())
            .map(|v| v.len())
            .sum()
    }
}

fn configured_archive() -> PathBuf {
    if let Ok(data) = fs::read_to_string(app_home().join("tool/.local/config.json")) {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&data) {
            if let Some(path) = value.get("archive").and_then(|v| v.as_str()) {
                return PathBuf::from(path);
            }
        }
    }
    app_home().join("backup")
}

fn load_catalog(root: &Path) -> Result<Catalog, String> {
    let root = root
        .canonicalize()
        .map_err(|e| format!("无法读取备份目录：{e}"))?;
    let mut catalog = Catalog::default();
    for entry in fs::read_dir(&root).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let branch = entry.file_name().to_string_lossy().to_string();
        if branch == "skills" {
            continue; // Skill package indexes are separate from conversation indexes.
        }
        let index = entry.path().join("index.json");
        if !index.is_file() {
            continue;
        }
        let value: serde_json::Value = match fs::read_to_string(&index)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
        {
            Some(value) => value,
            None => {
                catalog.warnings.push(format!("{branch}：索引无法读取"));
                continue;
            }
        };
        let Some(sessions) = value.get("sessions").and_then(|s| s.as_object()) else {
            catalog.warnings.push(format!("{branch}：索引缺少会话列表"));
            continue;
        };
        for (id, row) in sessions {
            let mut meta: SessionMeta = match serde_json::from_value(row.clone()) {
                Ok(value) => value,
                Err(e) => {
                    catalog.warnings.push(format!("{branch}/{id}：{e}"));
                    continue;
                }
            };
            if meta.id.is_empty() {
                meta.id = id.to_string();
            }
            let path = match entry.path().join(&meta.file).canonicalize() {
                Ok(path)
                    if path.starts_with(&root)
                        && path.extension().and_then(|s| s.to_str()) == Some("md") =>
                {
                    path
                }
                _ => {
                    catalog
                        .warnings
                        .push(format!("{branch}/{id}：会话文件缺失或路径无效"));
                    continue;
                }
            };
            let markdown = match fs::read_to_string(&path) {
                Ok(data) => data,
                Err(e) => {
                    catalog.warnings.push(format!("{branch}/{id}：{e}"));
                    continue;
                }
            };
            let device = branch
                .strip_suffix(&format!("-{}", meta.tool))
                .unwrap_or(&branch)
                .to_string();
            let search = format!(
                "{}\n{}\n{}\n{}\n{}",
                meta.title, meta.project, meta.id, device, markdown
            )
            .to_lowercase();
            let display_title = conversation_title(&meta.title, &markdown);
            catalog.sessions.push(Session {
                meta,
                display_title,
                branch: branch.clone(),
                device,
                path,
                markdown,
                search,
            });
        }
    }
    catalog.sessions.sort_by(|a, b| {
        b.meta
            .updated_at
            .cmp(&a.meta.updated_at)
            .then(a.meta.id.cmp(&b.meta.id))
    });
    Ok(catalog)
}

#[derive(Default)]
struct Filters {
    query: String,
    tool: String,
    device: String,
    project: String,
    model: String,
    label: String,
    source: String,
    archived: u8,
    days: i64,
}

impl Filters {
    fn matches(&self, session: &Session) -> bool {
        let m = &session.meta;
        if !self.tool.is_empty() && self.tool != m.tool {
            return false;
        }
        if !self.device.is_empty() && self.device != session.device {
            return false;
        }
        if !self.project.is_empty() && self.project != m.project {
            return false;
        }
        if !self.model.is_empty() && !m.models.contains(&self.model) {
            return false;
        }
        if !self.label.is_empty() && !m.labels.contains(&self.label) {
            return false;
        }
        if !self.source.is_empty() && self.source != m.source {
            return false;
        }
        // CLI sessions without an archive flag remain visible in the active tab.
        if self.archived == 1 && m.archived == Some(true) {
            return false;
        }
        if self.archived == 2 && m.archived != Some(true) {
            return false;
        }
        if self.days > 0
            && DateTime::parse_from_rfc3339(&m.updated_at)
                .map(|d| (Utc::now() - d.with_timezone(&Utc)).num_days() >= self.days)
                .unwrap_or(true)
        {
            return false;
        }
        self.query
            .to_lowercase()
            .split_whitespace()
            .all(|word| session.search.contains(word))
    }
}

fn prepare_markdown(markdown: &str, images: &Path, text_only: bool) -> String {
    // Only archived images are loaded. Remote/missing image links remain descriptive text.
    IMAGE_LINK
        .replace_all(markdown, |caps: &regex::Captures<'_>| {
            let alt = &caps[1];
            if text_only {
                return format!("〔图片：{alt}〕");
            }
            let Some(name) = caps[2].strip_prefix("../../../images/") else {
                return format!("〔外部图片：{alt}〕");
            };
            if name.contains(['/', '\\']) {
                return format!("〔无效图片：{alt}〕");
            }
            let path = images.canonicalize().ok().and_then(|root| {
                root.join(name)
                    .canonicalize()
                    .ok()
                    .filter(|p| p.starts_with(&root) && p.is_file())
            });
            let Some(path) = path else {
                return format!("〔图片文件缺失：{alt}，请检查图片目录〕");
            };
            match url::Url::from_file_path(path) {
                Ok(url) => format!(
                    "![{alt}](vault-{})",
                    url.as_str().replace('(', "%28").replace(')', "%29")
                ),
                Err(_) => format!("〔图片路径无效：{alt}〕"),
            }
        })
        .to_string()
}

fn body(markdown: &str) -> &str {
    if markdown.trim_start().starts_with("## ") {
        return markdown;
    }
    markdown
        .find("\n## ")
        .map(|i| &markdown[i + 1..])
        .unwrap_or(markdown)
}

struct MessageCard {
    role: &'static str,
    sender: String,
    timestamp: String,
    original: String,
    markdown: String,
    size: egui::Vec2,
    images: Vec<PreviewImage>,
    context: Option<&'static str>,
    context_image_count: usize,
    attachment_context: String,
    replies_expanded: bool,
}

fn separate_attachment_context(markdown: &str) -> (String, String) {
    if !markdown
        .trim_start()
        .starts_with("# Files mentioned by the user:")
    {
        return (markdown.to_string(), String::new());
    }
    let Some((files, request)) = markdown.split_once("\n## My request:") else {
        return (markdown.to_string(), String::new());
    };
    let mut context = files.trim().to_string();
    static IMAGE_WRAPPER: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"<image name=[^>\n]*>\s*</image>").unwrap());
    let request = IMAGE_WRAPPER.replace_all(request, |caps: &regex::Captures<'_>| {
        context.push_str("\n\n");
        context.push_str(&caps[0]);
        String::new()
    });
    (request.to_string(), context)
}

fn context_prefix(markdown: &str) -> Option<(&str, &'static str)> {
    let text = markdown.trim_start();
    if text.starts_with("# AGENTS.md instructions for ")
        || text.starts_with("AGENTS.md instructions for ")
    {
        text.split_once('\n')?
            .1
            .trim_start()
            .strip_prefix("<INSTRUCTIONS>")?;
        let end = text.find("</INSTRUCTIONS>")? + "</INSTRUCTIONS>".len();
        return Some((text[end..].trim(), "项目说明与上下文"));
    }
    let (tag, name) = [
        ("environment_context", "环境上下文"),
        ("external_codex_apps_open_page", "应用上下文"),
    ]
    .into_iter()
    .find(|(tag, _)| text.starts_with(&format!("<{tag}>")))?;
    let close = format!("</{tag}>");
    let end = text.find(&close)? + close.len();
    Some((text[end..].trim(), name))
}

fn context_only(markdown: &str) -> Option<&'static str> {
    let mut rest = markdown.trim();
    let mut label = None;
    while !rest.is_empty() {
        let (remaining, name) = context_prefix(rest)?;
        rest = remaining;
        label = label.or(Some(name));
    }
    label
}

fn auxiliary_title(title: &str) -> bool {
    let title = title.trim();
    title.is_empty()
        || title == "<environment_context>"
        || title == "<external_codex_apps_open_page>"
        || title == "# Files mentioned by the user:"
        || title.starts_with("# AGENTS.md instructions for ")
        || title.starts_with("AGENTS.md instructions for ")
        || context_only(title).is_some()
}

fn conversation_title(original: &str, markdown: &str) -> String {
    if !auxiliary_title(original) {
        return original.to_owned();
    }
    for card in message_cards(body(markdown)) {
        if card.role != "用户" || card.context.is_some() {
            continue;
        }
        let mut request = card.markdown.trim();
        while let Some((rest, _)) = context_prefix(request) {
            request = rest;
        }
        let (request, _) = separate_attachment_context(request);
        if let Some(line) = request.lines().map(str::trim).find(|line| {
            !line.is_empty() && !line.starts_with("![") && !line.starts_with("### 图片 · ")
        }) {
            // Only known wrappers are skipped; ambiguous markup stays in the message body.
            if line.starts_with('<') || auxiliary_title(line) {
                continue;
            }
            let mut title: String = line
                .trim_start_matches('#')
                .trim()
                .chars()
                .take(80)
                .collect();
            if line.chars().count() > 80 {
                title.push('…');
            }
            if !title.is_empty() {
                return title;
            }
        }
    }
    "未命名会话".to_string()
}

fn context_fields(markdown: &str) -> Vec<(&'static str, String)> {
    let mut fields = Vec::new();
    // These are display hints for known envelopes, not a general XML parser.
    let mut rest = markdown.trim();
    while let Some((remaining, _)) = context_prefix(rest) {
        if let Some(environment) = rest
            .strip_prefix("<environment_context>")
            .and_then(|rest| rest.split_once("</environment_context>"))
            .map(|(environment, _)| environment)
        {
            for (tag, label) in [
                ("cwd", "工作目录"),
                ("shell", "终端"),
                ("current_date", "日期"),
                ("timezone", "时区"),
                ("root", "工作区"),
            ] {
                let values: Vec<_> = environment
                    .split(&format!("<{tag}>"))
                    .skip(1)
                    .filter_map(|rest| rest.split_once(&format!("</{tag}>")))
                    .map(|(value, _)| value.trim())
                    .filter(|value| !value.is_empty() && !value.contains('<'))
                    .collect();
                if !values.is_empty() {
                    fields.push((label, values.join("\n")));
                }
            }
        }
        if let Some(page) = rest
            .strip_prefix("<external_codex_apps_open_page>")
            .and_then(|rest| rest.split_once("</external_codex_apps_open_page>"))
            .map(|(json, _)| json)
            .and_then(|json| serde_json::from_str::<serde_json::Value>(json).ok())
        {
            if let Some(id) = page.get("page_id") {
                fields.push((
                    "关联页面",
                    if id.is_null() {
                        "无".to_string()
                    } else {
                        id.as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| id.to_string())
                    },
                ));
            }
        }
        rest = remaining;
    }
    fields
}

fn show_context(ui: &mut egui::Ui, card: &MessageCard) {
    let fields = context_fields(&card.original);
    if fields.is_empty() {
        ui.label(
            RichText::new("会话附带的说明与上下文，可按需查看原文。")
                .small()
                .color(MUTED),
        );
    } else {
        for (label, value) in fields {
            ui.horizontal_top(|ui| {
                ui.add_sized(
                    [64.0, 20.0],
                    egui::Label::new(RichText::new(label).small().color(MUTED)),
                );
                ui.add(egui::Label::new(RichText::new(value).size(13.0)).wrap());
            });
        }
    }
    if ui.small_button("复制原文").clicked() {
        ui.ctx().copy_text(card.original.clone());
    }
    egui::ScrollArea::vertical()
        .id_salt("context-original-scroll")
        .max_height(260.0)
        .show(ui, |ui| {
            ui.add(egui::Label::new(RichText::new(&card.original).monospace().size(12.0)).wrap());
        });
}

#[derive(Clone)]
struct PreviewImage {
    uri: String,
    alt: String,
}

fn thumbnail_markdown(markdown: &str) -> (String, Vec<PreviewImage>) {
    // Use the existing Markdown parser so code examples never become thumbnails.
    let mut images = Vec::new();
    let mut rendered = String::new();
    let mut end = 0;
    for (event, range) in
        pulldown_cmark::Parser::new_ext(markdown, pulldown_cmark::Options::all()).into_offset_iter()
    {
        if let pulldown_cmark::Event::Start(pulldown_cmark::Tag::Image { dest_url, .. }) = event {
            if !dest_url.starts_with("vault-file://") || range.start < end {
                continue;
            }
            let before = &markdown[end..range.start];
            if let Some((text, date)) =
                before
                    .rsplit_once("\n### 图片 · ")
                    .and_then(|(text, date)| {
                        DateTime::parse_from_rfc3339(date.trim())
                            .ok()
                            .map(|date| (text, date))
                    })
            {
                rendered.push_str(text);
                rendered.push_str(&format!(
                    "\n\n*图片 · {}*\n\n",
                    date.with_timezone(&Local).format("%m-%d %H:%M")
                ));
            } else {
                rendered.push_str(before);
            }
            rendered.push_str(&format!("\n\n<vault-thumbnail-{}>\n\n", images.len()));
            let alt = pulldown_cmark::Parser::new(&markdown[range.clone()])
                .filter_map(|e| match e {
                    pulldown_cmark::Event::Text(s) | pulldown_cmark::Event::Code(s) => {
                        Some(s.to_string())
                    }
                    _ => None,
                })
                .collect::<String>();
            images.push(PreviewImage {
                uri: dest_url.to_string(),
                alt,
            });
            end = range.end;
        }
    }
    rendered.push_str(&markdown[end..]);
    (rendered, images)
}

fn message_cards(markdown: &str) -> Vec<MessageCard> {
    let mut cards: Vec<MessageCard> = Vec::new();
    let mut fence: Option<(char, usize)> = None;
    for line in markdown.split_inclusive('\n') {
        let text = line.trim_end_matches(['\r', '\n']);
        let heading = if fence.is_none() {
            text.strip_prefix("## ")
                .and_then(|s| s.split_once(" · "))
                .and_then(|(role, date)| match role {
                    "用户" | "user" => Some(("用户", date)),
                    "助手" | "assistant" => Some(("助手", date)),
                    _ => None,
                })
        } else {
            None
        };
        if let Some((role, date)) = heading {
            cards.push(MessageCard {
                role,
                sender: if role == "用户" { "你" } else { "助手" }.into(),
                timestamp: date.to_owned(),
                original: String::new(),
                markdown: String::new(),
                size: egui::vec2(0.0, 140.0),
                images: Vec::new(),
                context: None,
                context_image_count: 0,
                attachment_context: String::new(),
                replies_expanded: false,
            });
            continue;
        }
        let trimmed = text.trim_start_matches(' ');
        if text.len() - trimmed.len() <= 3 {
            if let Some(marker @ ('`' | '~')) = trimmed.chars().next() {
                let count = trimmed.chars().take_while(|c| *c == marker).count();
                if let Some((open, length)) = fence {
                    if marker == open && count >= length && trimmed[count..].trim().is_empty() {
                        fence = None;
                    }
                } else if count >= 3 {
                    fence = Some((marker, count));
                }
            }
        }
        if cards.is_empty() {
            cards.push(MessageCard {
                role: "原文 / 附件",
                sender: "附件".into(),
                timestamp: String::new(),
                original: String::new(),
                markdown: String::new(),
                size: egui::vec2(0.0, 140.0),
                images: Vec::new(),
                context: None,
                context_image_count: 0,
                attachment_context: String::new(),
                replies_expanded: false,
            });
        }
        cards.last_mut().unwrap().markdown.push_str(line);
    }
    cards.retain(|c| !c.markdown.trim().is_empty() || !c.timestamp.is_empty());
    for card in &mut cards {
        card.original = card.markdown.clone();
        if card.role == "用户" {
            card.context = context_only(&card.markdown);
            (card.markdown, card.attachment_context) = separate_attachment_context(&card.markdown);
            if let Some(request) = message_metadata::human_request(&card.markdown) {
                card.attachment_context = card.original.clone();
                card.markdown = request;
            }
        }
    }
    cards
}

fn assistant_reply_groups(cards: &[MessageCard]) -> BTreeMap<usize, (usize, usize)> {
    let mut groups = BTreeMap::new();
    let mut start = 0;
    while start < cards.len() {
        if cards[start].role != "助手" {
            start += 1;
            continue;
        }
        let (mut end, mut latest, mut replies) = (start, start, 0);
        while end < cards.len() && (cards[end].role == "助手" || cards[end].context.is_some()) {
            if cards[end].role == "助手" {
                latest = end;
                replies += 1;
            }
            end += 1;
        }
        if replies > 1 {
            // Keep the original last reply visible; older archives have no final-channel marker.
            groups.insert(start, (latest, replies - 1));
        }
        start = end;
    }
    groups
}

fn show_message_cards(
    ui: &mut egui::Ui,
    cards: &mut [MessageCard],
    cache: &mut CommonMarkCache,
    thumbnail_width: u32,
    focus_message: Option<usize>,
    scroll_target: &mut Option<usize>,
) -> Option<PreviewImage> {
    let clicked = Rc::new(RefCell::new(None));
    let reply_groups = assistant_reply_groups(cards);
    egui::ScrollArea::both()
        .id_salt("message-cards")
        .auto_shrink([false, false])
        .show_viewport(ui, |ui, viewport| {
            let available = ui.available_width();
            let width = (available - 24.0).clamp(100.0, 760.0);
            let top = ui.cursor().top();
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = 0.0;
                ui.add_space(((available - width) * 0.5).max(0.0));
                ui.vertical(|ui| {
                    ui.set_width(width);
                    ui.spacing_mut().item_spacing.y = 0.0;
                    ui.add_space(20.0);
                    let mut hidden_until = 0;
                    for (index, card) in cards.iter_mut().enumerate() {
                        if let Some(&(latest, count)) = reply_groups.get(&index) {
                            if scroll_target.is_some_and(|target| (index..latest).contains(&target))
                            {
                                card.replies_expanded = true;
                            }
                            let label = if card.replies_expanded {
                                format!("收起过程回复 · {count} 条")
                            } else {
                                format!("展开过程回复 · {count} 条")
                            };
                            if ui
                                .push_id(("reply-group", index), |ui| {
                                    ui.add(
                                        egui::Button::new(
                                            RichText::new(label).size(12.0).color(MUTED),
                                        )
                                        .frame(false),
                                    )
                                    .on_hover_text(
                                        "本轮最后一条回复保持展示，展开可查看较早的回复。",
                                    )
                                    .clicked()
                                })
                                .inner
                            {
                                card.replies_expanded = !card.replies_expanded;
                            }
                            if !card.replies_expanded {
                                hidden_until = latest;
                            }
                        }
                        if index < hidden_until {
                            continue;
                        }
                        let y = ui.cursor().top() - top;
                        let height = if card.size.x == width {
                            card.size.y
                        } else {
                            140.0
                        };
                        if *scroll_target != Some(index)
                            && (y + height < viewport.min.y - 200.0 || y > viewport.max.y + 200.0)
                        {
                            ui.allocate_space(egui::vec2(width, height));
                            continue;
                        }
                        let response = ui
                            .push_id(index, |ui| {
                                ui.vertical(|ui| {
                                    if card.context.is_some() {
                                        ui.add_space(4.0);
                                        egui::Frame::new()
                                            .fill(PAPER)
                                            .corner_radius(7)
                                            .inner_margin(egui::Margin::symmetric(10, 3))
                                            .show(ui, |ui| {
                                                ui.set_width(width - 20.0);
                                                ui.spacing_mut().item_spacing.y = 8.0;
                                                let time = short_date(&card.timestamp);
                                                let label = format!(
                                                    "{}{}{}",
                                                    message_metadata::title(card),
                                                    if card.context_image_count == 0 {
                                                        String::new()
                                                    } else {
                                                        format!(
                                                            "  ·  {} 张关联图片",
                                                            card.context_image_count
                                                        )
                                                    },
                                                    if time.is_empty() {
                                                        String::new()
                                                    } else {
                                                        format!("  ·  {time}")
                                                    }
                                                );
                                                egui::CollapsingHeader::new(
                                                    RichText::new(label).size(12.0).color(MUTED),
                                                )
                                                .id_salt("auxiliary-content")
                                                .default_open(focus_message == Some(index))
                                                .show(ui, |ui| {
                                                    show_context(ui, card);
                                                    if card.context_image_count > 0 {
                                                        egui::CollapsingHeader::new(format!(
                                                            "关联图片 · {} 张",
                                                            card.context_image_count
                                                        ))
                                                        .id_salt("context-images")
                                                        .show(ui, |ui| {
                                                            egui::ScrollArea::vertical()
                                                                .max_height(360.0)
                                                                .id_salt("context-images-scroll")
                                                                .show(ui, |ui| {
                                                                    show_message_body(
                                                                        ui,
                                                                        card,
                                                                        cache,
                                                                        thumbnail_width,
                                                                        &clicked,
                                                                    )
                                                                });
                                                        });
                                                    }
                                                });
                                            });
                                        return;
                                    }
                                    ui.add_space(26.0);
                                    let user_message = card.role == "用户";
                                    let message_width = if user_message {
                                        (width * 0.86).min(640.0)
                                    } else {
                                        width
                                    };
                                    ui.horizontal_top(|ui| {
                                        ui.spacing_mut().item_spacing.x = 0.0;
                                        ui.add_space(width - message_width);
                                        ui.vertical(|ui| {
                                            ui.set_width(message_width);
                                            ui.spacing_mut().item_spacing = egui::vec2(8.0, 7.0);
                                            ui.horizontal(|ui| {
                                                ui.label(
                                                    RichText::new(&card.sender)
                                                        .size(12.0)
                                                        .strong()
                                                        .color(INK),
                                                );
                                                ui.label(
                                                    RichText::new(short_date(&card.timestamp))
                                                        .size(11.0)
                                                        .color(MUTED),
                                                )
                                                .on_hover_text(&card.timestamp);
                                                ui.with_layout(
                                                    egui::Layout::right_to_left(
                                                        egui::Align::Center,
                                                    ),
                                                    |ui| {
                                                        if ui
                                                            .add(
                                                                egui::Button::new(
                                                                    RichText::new("复制")
                                                                        .size(11.0)
                                                                        .color(MUTED),
                                                                )
                                                                .small()
                                                                .frame(false),
                                                            )
                                                            .on_hover_text("复制这条消息的完整原文")
                                                            .clicked()
                                                        {
                                                            ui.ctx()
                                                                .copy_text(card.original.clone());
                                                        }
                                                    },
                                                );
                                            });
                                            egui::Frame::new()
                                                .fill(if user_message {
                                                    Color32::from_rgb(243, 243, 245)
                                                } else {
                                                    Color32::TRANSPARENT
                                                })
                                                .stroke(if focus_message == Some(index) {
                                                    egui::Stroke::new(1.5, ACCENT)
                                                } else {
                                                    egui::Stroke::NONE
                                                })
                                                .corner_radius(if user_message { 14 } else { 6 })
                                                .inner_margin(if user_message { 16 } else { 0 })
                                                .show(ui, |ui| {
                                                    ui.set_width(
                                                        (message_width
                                                            - if user_message {
                                                                32.0
                                                            } else {
                                                                0.0
                                                            })
                                                        .max(66.0),
                                                    );
                                                    show_message_body(
                                                        ui,
                                                        card,
                                                        cache,
                                                        thumbnail_width,
                                                        &clicked,
                                                    );
                                                });
                                            if !card.attachment_context.is_empty() {
                                                egui::CollapsingHeader::new(
                                                    RichText::new("消息来源 · 原始信息")
                                                        .size(11.0)
                                                        .color(MUTED),
                                                )
                                                .id_salt("attachment-context")
                                                .show(ui, |ui| {
                                                    egui::ScrollArea::vertical()
                                                        .max_height(220.0)
                                                        .show(ui, |ui| {
                                                            ui.add(
                                                                egui::Label::new(
                                                                    RichText::new(
                                                                        &card.attachment_context,
                                                                    )
                                                                    .monospace()
                                                                    .size(12.0),
                                                                )
                                                                .wrap(),
                                                            );
                                                        });
                                                });
                                            }
                                        });
                                    });
                                })
                                .response
                            })
                            .inner;
                        card.size = egui::vec2(width, response.rect.height());
                        if *scroll_target == Some(index) {
                            if y >= viewport.min.y && y < viewport.max.y - 40.0 {
                                *scroll_target = None;
                            } else {
                                ui.scroll_to_rect(response.rect, Some(egui::Align::Min));
                                ui.ctx().request_repaint();
                            }
                        }
                    }
                    ui.add_space(32.0);
                });
            });
        });
    let selected = clicked.borrow_mut().take();
    selected
}

fn show_message_body(
    ui: &mut egui::Ui,
    card: &MessageCard,
    cache: &mut CommonMarkCache,
    thumbnail_width: u32,
    clicked: &Rc<RefCell<Option<PreviewImage>>>,
) {
    let max_width = (thumbnail_width as f32).min((ui.available_width() - 16.0).max(48.0));
    ui.visuals_mut().extreme_bg_color = PAPER;
    ui.visuals_mut().widgets.noninteractive.corner_radius = egui::CornerRadius::same(8);
    let pictures = card.images.clone();
    let clicked = Rc::clone(clicked);
    CommonMarkViewer::new()
        .render_html_fn(Some(&move |ui, html| {
            let image = html
                .trim()
                .strip_prefix("<vault-thumbnail-")
                .and_then(|s| s.strip_suffix('>'))
                .and_then(|s| s.parse::<usize>().ok())
                .and_then(|i| pictures.get(i));
            if let Some(image) = image {
                egui::Frame::new()
                    .fill(Color32::WHITE)
                    .stroke(egui::Stroke::new(1.0, LINE))
                    .corner_radius(10)
                    .inner_margin(7)
                    .show(ui, |ui| {
                        let response = ui
                            .add(
                                egui::Image::new(&image.uri)
                                    .fit_to_original_size(1.0)
                                    .max_size(egui::vec2(max_width, thumbnail_width as f32 * 0.75))
                                    .corner_radius(6)
                                    .alt_text(&image.alt)
                                    .sense(egui::Sense::click()),
                            )
                            .on_hover_cursor(egui::CursorIcon::ZoomIn)
                            .on_hover_text("点击查看大图");
                        if response.clicked() {
                            clicked.replace(Some(image.clone()));
                        }
                    });
            } else {
                ui.label(html);
            }
        }))
        .show(ui, cache, &card.markdown);
}

fn short_date(date: &str) -> String {
    DateTime::parse_from_rfc3339(date)
        .map(|d| d.with_timezone(&Local).format("%m-%d %H:%M").to_string())
        .unwrap_or_default()
}

fn reveal(path: &Path) {
    #[cfg(target_os = "macos")]
    {
        let _ = Command::new("open").arg("-R").arg(path).spawn();
    }
    #[cfg(target_os = "windows")]
    {
        let _ = Command::new("explorer")
            .arg(format!("/select,{}", path.display()))
            .spawn();
    }
    #[cfg(target_os = "linux")]
    {
        let _ = Command::new("xdg-open")
            .arg(path.parent().unwrap_or(path))
            .spawn();
    }
}

fn backup_tool() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("CHAT_HISTORY_VAULT_TOOL") {
        let path = PathBuf::from(path);
        let script = if path.is_dir() {
            path.join("vault.py")
        } else {
            path
        };
        return script
            .canonicalize()
            .and_then(|path| {
                if path.is_file() {
                    Ok(path)
                } else {
                    Err(std::io::Error::new(
                        std::io::ErrorKind::NotFound,
                        "vault.py",
                    ))
                }
            })
            .map_err(|_| "CHAT_HISTORY_VAULT_TOOL 未指向有效的 vault.py".into());
    }
    let bundled = std::env::current_exe().ok().and_then(|p| {
        p.parent()?
            .parent()
            .map(|p| p.join("Resources/tool/vault.py"))
    });
    bundled
        .into_iter()
        .chain([
            app_home().join("tool/vault.py"),
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../vault.py"),
        ])
        .into_iter()
        .find(|path| path.is_file())
        .ok_or_else(|| "未找到备份工具 vault.py，可通过 CHAT_HISTORY_VAULT_TOOL 指定。".to_string())
        .and_then(|path| path.canonicalize().map_err(|error| error.to_string()))
}

fn run_backup_tool(args: &[String]) -> Result<serde_json::Value, String> {
    let script = backup_tool()?;
    let candidates: &[(&str, &[&str])] = if cfg!(target_os = "windows") {
        &[("py", &["-3"]), ("python", &[]), ("python3", &[])]
    } else {
        &[
            ("/opt/homebrew/opt/starvault/libexec/bin/python", &[]),
            ("/usr/local/opt/starvault/libexec/bin/python", &[]),
            ("python3", &[]),
        ]
    };
    for &(python, prefix) in candidates {
        let output = Command::new(python)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .args(prefix)
            .arg(&script)
            .args(args)
            .current_dir(script.parent().unwrap())
            .output();
        let output = match output {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(format!("无法启动 {python}：{error}")),
            Ok(output) => output,
        };
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let stdout = String::from_utf8_lossy(&output.stdout);
            let detail = if stderr.trim().is_empty() {
                stdout
            } else {
                stderr
            };
            return Err(format!(
                "备份工具退出（{}）：{}",
                output.status,
                detail.chars().take(3000).collect::<String>()
            ));
        }
        return String::from_utf8_lossy(&output.stdout)
            .lines()
            .rev()
            .find_map(|line| serde_json::from_str(line).ok())
            .ok_or_else(|| "备份工具未返回结果".to_string());
    }
    Err("未找到 Python 3，请安装 Python 3 并将其加入 PATH。".into())
}

fn spawn_backup_job(
    ctx: &egui::Context,
    args: Vec<String>,
) -> mpsc::Receiver<Result<serde_json::Value, String>> {
    let (tx, rx) = mpsc::channel();
    let ctx = ctx.clone();
    thread::spawn(move || {
        let _ = tx.send(run_backup_tool(&args));
        ctx.request_repaint();
    });
    rx
}

#[derive(Clone, Copy, PartialEq)]
enum SkillJob {
    Plan,
    Backup,
    List,
    Restore,
    Forget,
}

fn setup(ctx: &egui::Context, font_size: f32) {
    ctx.set_theme(egui::Theme::Light);
    egui_extras::install_image_loaders(ctx);
    ctx.add_bytes_loader(Arc::new(LocalImageUrls::default()));
    let mut fonts = egui::FontDefinitions::default();
    let candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ];
    for path in candidates {
        if let Ok(data) = fs::read(path) {
            fonts
                .font_data
                .insert("cjk".into(), egui::FontData::from_owned(data).into());
            fonts
                .families
                .get_mut(&egui::FontFamily::Proportional)
                .unwrap()
                .push("cjk".into());
            fonts
                .families
                .get_mut(&egui::FontFamily::Monospace)
                .unwrap()
                .push("cjk".into());
            break;
        }
    }
    ctx.set_fonts(fonts);
    let mut style = (*ctx.style()).clone();
    style.visuals = egui::Visuals::light();
    style.visuals.panel_fill = PAPER;
    style.visuals.window_fill = Color32::WHITE;
    style.visuals.override_text_color = Some(INK);
    style.visuals.selection.bg_fill = SELECTED;
    style.visuals.selection.stroke.color = ACCENT;
    style.visuals.hyperlink_color = ACCENT;
    style.visuals.extreme_bg_color = Color32::WHITE;
    style.visuals.widgets.noninteractive.bg_stroke = egui::Stroke::new(1.0, LINE);
    for widget in [
        &mut style.visuals.widgets.inactive,
        &mut style.visuals.widgets.hovered,
        &mut style.visuals.widgets.active,
        &mut style.visuals.widgets.open,
    ] {
        widget.corner_radius = egui::CornerRadius::same(8);
        widget.bg_stroke = egui::Stroke::new(1.0, LINE);
        widget.fg_stroke.color = INK;
        widget.expansion = 0.0;
    }
    style.visuals.widgets.inactive.bg_fill = Color32::WHITE;
    style.visuals.widgets.inactive.weak_bg_fill = Color32::WHITE;
    style.visuals.widgets.hovered.bg_fill = HOVER;
    style.visuals.widgets.hovered.weak_bg_fill = HOVER;
    style.visuals.widgets.active.bg_fill = SELECTED;
    style.visuals.widgets.active.weak_bg_fill = SELECTED;
    style.spacing.item_spacing = egui::vec2(8.0, 8.0);
    style.spacing.button_padding = egui::vec2(9.0, 5.0);
    style.spacing.interact_size.y = 28.0;
    style
        .text_styles
        .insert(egui::TextStyle::Body, egui::FontId::proportional(font_size));
    style
        .text_styles
        .insert(egui::TextStyle::Button, egui::FontId::proportional(14.0));
    style
        .text_styles
        .insert(egui::TextStyle::Heading, egui::FontId::proportional(21.0));
    style
        .text_styles
        .insert(egui::TextStyle::Small, egui::FontId::proportional(11.5));
    style
        .text_styles
        .insert(egui::TextStyle::Monospace, egui::FontId::monospace(13.0));
    ctx.set_style(style);
}

#[derive(Clone, Copy, Default, PartialEq)]
enum SettingsTab {
    #[default]
    Images,
    Share,
    About,
}

fn settings_layout(
    ui: &mut egui::Ui,
    mut selected: SettingsTab,
    content: impl FnOnce(&mut egui::Ui, SettingsTab),
) -> SettingsTab {
    let height = ui.available_height().clamp(280.0, 660.0);
    ui.horizontal_top(|ui| {
        egui::Frame::new()
            .fill(PAPER)
            .corner_radius(10)
            .inner_margin(10)
            .show(ui, |ui| {
                ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                    ui.set_width(140.0);
                    ui.set_min_height(height - 20.0);
                    ui.label(RichText::new("设置分类").small().color(MUTED));
                    ui.add_space(8.0);
                    for (tab, title) in [
                        (SettingsTab::Images, "图片显示"),
                        (SettingsTab::Share, "分享软件"),
                        (SettingsTab::About, "关于与反馈"),
                    ] {
                        if ui
                            .add_sized(
                                [140.0, 40.0],
                                egui::SelectableLabel::new(selected == tab, title),
                            )
                            .clicked()
                        {
                            selected = tab;
                        }
                    }
                });
            });
        ui.add_space(8.0);
        ui.vertical(|ui| {
            ui.set_width(ui.available_width());
            egui::ScrollArea::vertical()
                .id_salt(("settings-content", selected as u8))
                .max_height(height)
                .min_scrolled_height(height)
                .auto_shrink([false, false])
                .show(ui, |ui| content(ui, selected));
        });
    });
    selected
}

struct VaultApp {
    root: PathBuf,
    catalog: Catalog,
    filters: Filters,
    visible: Vec<usize>,
    selected: Option<usize>,
    cards: Vec<MessageCard>,
    cache: CommonMarkCache,
    receiver: Option<mpsc::Receiver<Result<Catalog, String>>>,
    prefs: Preferences,
    status: String,
    last_refresh: Instant,
    oldest: bool,
    show_details: bool,
    screenshot_select: Option<String>,
    show_settings: bool,
    settings_tab: SettingsTab,
    share: share::Panel,
    images_input: String,
    missing_images: usize,
    screenshot: Option<PathBuf>,
    capture_after: Option<Instant>,
    capture_requested: bool,
    show_resources: bool,
    attachment_filters: attachments_view::Filters,
    attachment_files: Vec<attachments_view::Attachment>,
    manifest: serde_json::Value,
    migration_kinds: BTreeSet<String>,
    migrate_all: bool,
    migration_saved_kinds: BTreeSet<String>,
    migration_plan: Option<serde_json::Value>,
    job_receiver: Option<mpsc::Receiver<Result<serde_json::Value, String>>>,
    job_is_plan: bool,
    show_skills: bool,
    skill_sources: String,
    skill_defaults: bool,
    remember_skills: bool,
    remembered_skills: bool,
    skill_settings_loaded: bool,
    skill_report: Option<serde_json::Value>,
    skill_report_is_plan: bool,
    skill_receiver: Option<mpsc::Receiver<Result<serde_json::Value, String>>>,
    skill_job: SkillJob,
    skill_status: String,
    skill_error: bool,
    preview_image: Option<PreviewImage>,
    preview_zoom: Option<f32>,
    preview_first: bool,
    timeline_kind: String,
    timeline_newest: bool,
    focus_message: Option<usize>,
    scroll_target: Option<usize>,
}

impl VaultApp {
    fn new(
        cc: &eframe::CreationContext<'_>,
        root: PathBuf,
        prefs: Preferences,
        select: Option<String>,
        screenshot: Option<PathBuf>,
        filters: Filters,
    ) -> Self {
        setup(&cc.egui_ctx, prefs.font_size);
        let root = if root.is_absolute() {
            root
        } else {
            std::env::current_dir().unwrap_or_default().join(root)
        };
        let images_input = prefs.images.clone();
        let migration_kinds: BTreeSet<String> = fs::read_to_string(root.join(".migration.json"))
            .ok()
            .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
            .and_then(|v| {
                v["kinds"].as_array().map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str().map(str::to_string))
                        .collect()
                })
            })
            .unwrap_or_else(|| ["images".to_string()].into_iter().collect());
        let mut app = Self {
            root,
            prefs,
            catalog: Catalog::default(),
            filters,
            visible: vec![],
            selected: None,
            cards: Vec::new(),
            cache: CommonMarkCache::default(),
            receiver: None,
            status: "正在读取本地备份…".into(),
            last_refresh: Instant::now(),
            oldest: false,
            show_details: false,
            screenshot_select: select,
            show_settings: false,
            settings_tab: SettingsTab::default(),
            share: share::Panel::default(),
            images_input,
            missing_images: 0,
            screenshot,
            capture_after: None,
            capture_requested: false,
            show_resources: false,
            attachment_filters: attachments_view::Filters::default(),
            attachment_files: Vec::new(),
            manifest: serde_json::Value::Null,
            migration_saved_kinds: migration_kinds.clone(),
            migration_kinds,
            migrate_all: false,
            migration_plan: None,
            job_receiver: None,
            job_is_plan: false,
            show_skills: false,
            skill_sources: String::new(),
            skill_defaults: true,
            remember_skills: false,
            remembered_skills: false,
            skill_settings_loaded: false,
            skill_report: None,
            skill_report_is_plan: false,
            skill_receiver: None,
            skill_job: SkillJob::List,
            skill_status: String::new(),
            skill_error: false,
            preview_image: None,
            preview_zoom: None,
            preview_first: false,
            timeline_kind: String::new(),
            timeline_newest: false,
            focus_message: None,
            scroll_target: None,
        };
        app.reload(&cc.egui_ctx);
        app
    }

    fn reload(&mut self, ctx: &egui::Context) {
        if self.receiver.is_some() {
            return;
        }
        let (tx, rx) = mpsc::channel();
        let root = self.root.clone();
        let context = ctx.clone();
        self.receiver = Some(rx);
        self.last_refresh = Instant::now();
        thread::spawn(move || {
            let result = load_catalog(&root).map(|mut catalog| {
                let (nodes, stats, warnings) = timeline::refresh(&root, &catalog.sessions);
                catalog.timelines = nodes;
                catalog.timeline_stats = stats;
                catalog.warnings.extend(warnings);
                catalog
            });
            let _ = tx.send(result);
            context.request_repaint();
        });
    }

    fn poll(&mut self, ctx: &egui::Context) {
        if let Some(result) = self.receiver.as_ref().and_then(|rx| rx.try_recv().ok()) {
            self.receiver = None;
            match result {
                Ok(catalog) => {
                    let expanded_replies: Vec<_> = self
                        .cards
                        .iter()
                        .enumerate()
                        .filter(|(_, card)| card.replies_expanded)
                        .map(|(index, card)| (index, card.timestamp.clone()))
                        .collect();
                    let key = self
                        .selected
                        .and_then(|i| self.catalog.sessions.get(i))
                        .map(|s| (s.branch.clone(), s.meta.id.clone()));
                    self.catalog = catalog;
                    self.status = format!(
                        "{} 个会话 · 本地读取 · {} 更新",
                        self.catalog.sessions.len(),
                        Local::now().format("%H:%M")
                    );
                    self.selected = key.as_ref().and_then(|(b, id)| {
                        self.catalog
                            .sessions
                            .iter()
                            .position(|s| &s.branch == b && &s.meta.id == id)
                    });
                    if let Some(select) = self.screenshot_select.take() {
                        self.selected = self
                            .catalog
                            .sessions
                            .iter()
                            .position(|s| s.meta.id == select);
                    }
                    self.refilter();
                    if self.selected.is_none() {
                        self.selected = self.visible.first().copied();
                    }
                    let same_conversation = self.selected.is_some_and(|i| {
                        key.as_ref().is_some_and(|(branch, id)| {
                            &self.catalog.sessions[i].branch == branch
                                && &self.catalog.sessions[i].meta.id == id
                        })
                    });
                    self.render_selected(ctx);
                    if same_conversation {
                        // An automatic index refresh must not close what the reader just expanded.
                        for (index, timestamp) in expanded_replies {
                            if let Some(card) = self.cards.get_mut(index) {
                                if card.timestamp == timestamp {
                                    card.replies_expanded = true;
                                }
                            }
                        }
                    }
                    if self.preview_first {
                        self.preview_image = self
                            .cards
                            .iter()
                            .flat_map(|card| &card.images)
                            .next()
                            .cloned();
                        self.preview_first = false;
                    }
                    self.capture_after = Some(Instant::now() + Duration::from_secs(3));
                }
                Err(error) => self.status = error,
            }
        }
    }

    fn refilter(&mut self) {
        self.visible = self
            .catalog
            .sessions
            .iter()
            .enumerate()
            .filter_map(|(i, s)| self.filters.matches(s).then_some(i))
            .collect();
        if self.oldest {
            self.visible.reverse();
        }
    }

    fn render_selected(&mut self, ctx: &egui::Context) {
        self.focus_message = None;
        self.scroll_target = None;
        // The gallery can contain thousands of images. Release previous conversation textures.
        ctx.forget_all_images();
        self.cache = CommonMarkCache::default();
        self.manifest = self
            .selected
            .and_then(|i| self.local_record_path(i))
            .and_then(|p| fs::read_to_string(p).ok())
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default();
        self.migration_plan = None;
        let images = image_directory(&self.root, &self.prefs.images);
        self.attachment_files = attachments_view::entries(
            &self.root,
            &self.manifest,
            (!self.prefs.images.is_empty()).then_some(images.as_path()),
        );
        self.missing_images = self
            .selected
            .map(|i| {
                self.catalog.sessions[i]
                    .meta
                    .assets
                    .iter()
                    .filter(|name| !images.join(name).is_file())
                    .count()
            })
            .unwrap_or(0);
        self.cards = self
            .selected
            .map(|i| message_cards(body(&self.catalog.sessions[i].markdown)))
            .unwrap_or_default();
        if let Some(path) = self.selected.and_then(|i| self.local_record_path(i)) {
            message_metadata::apply(&mut self.cards, &path);
        }
        for card in &mut self.cards {
            if card.role == "助手" {
                card.sender = self
                    .selected
                    .map(|i| tool_name(&self.catalog.sessions[i].meta.tool))
                    .unwrap_or_else(|| "助手".into());
            }
            (card.markdown, card.images) = thumbnail_markdown(&prepare_markdown(
                &card.markdown,
                &images,
                self.prefs.text_only,
            ));
        }
        if self.preview_image.as_ref().is_some_and(|preview| {
            !self
                .cards
                .iter()
                .any(|card| card.images.iter().any(|image| image.uri == preview.uri))
        }) {
            self.preview_image = None;
            self.preview_zoom = None;
        }
    }

    fn choose_archive(&mut self, ctx: &egui::Context) {
        if let Some(path) = rfd::FileDialog::new()
            .set_directory(&self.root)
            .pick_folder()
        {
            // Discard a refresh for the previous archive before starting this one.
            self.receiver = None;
            self.root = path;
            self.selected = None;
            self.filters = Filters::default();
            self.catalog = Catalog::default();
            self.visible.clear();
            self.cards.clear();
            self.skill_report = None;
            self.skill_settings_loaded = false;
            self.skill_sources.clear();
            self.skill_defaults = true;
            self.remember_skills = false;
            self.remembered_skills = false;
            self.skill_status.clear();
            self.skill_error = false;
            self.save_preferences();
            self.reload(ctx);
            if self.show_skills {
                self.start_skill_job(ctx, SkillJob::List, vec![]);
            }
        }
    }

    fn save_preferences(&mut self) {
        self.prefs.archive = self.root.to_string_lossy().to_string();
        let path = app_home().join("viewer.json");
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(data) = serde_json::to_string_pretty(&self.prefs) {
            if let Err(error) = fs::write(path, data) {
                self.status = format!("设置保存失败：{error}");
            }
        }
    }

    fn apply_filters(&mut self, ctx: &egui::Context) {
        self.refilter();
        if !self.selected.is_some_and(|i| self.visible.contains(&i)) {
            self.selected = self.visible.first().copied();
            self.render_selected(ctx);
        }
    }

    fn project_timeline(&mut self, ui: &mut egui::Ui, selected: usize) -> Option<(usize, usize)> {
        let session = &self.catalog.sessions[selected];
        let identity = project_identity(session);
        let rows: Vec<usize> = self
            .visible
            .iter()
            .copied()
            .filter(|&i| project_identity(&self.catalog.sessions[i]) == identity)
            .collect();
        let total: usize = rows.iter().map(|&i| self.catalog.timelines[i].len()).sum();
        ui.label(RichText::new("项目时间线").small().color(ACCENT));
        ui.add(
            egui::Label::new(RichText::new(&session.meta.project).size(25.0).strong()).truncate(),
        )
        .on_hover_text(&session.meta.project);
        ui.add(
            egui::Label::new(
                RichText::new(format!(
                    "{} · {} · {}",
                    session.device,
                    session.meta.tool.to_uppercase(),
                    session.meta.cwd
                ))
                .small()
                .color(MUTED),
            )
            .truncate(),
        )
        .on_hover_text(&session.meta.cwd);
        ui.add_space(8.0);
        ui.label(format!(
            "当前筛选内 {} 段会话 · {} 个关键节点",
            rows.len(),
            total
        ));
        ui.label(
            RichText::new("从对话原句自动摘录 · 按规则分类，点击节点核对原文")
                .small()
                .color(MUTED),
        );
        ui.horizontal_wrapped(|ui| {
            for kind in ["", "需求", "决定", "成果", "问题", "修复"] {
                ui.selectable_value(
                    &mut self.timeline_kind,
                    kind.into(),
                    if kind.is_empty() {
                        "全部节点"
                    } else {
                        kind
                    },
                );
            }
            if ui
                .button(if self.timeline_newest {
                    "最新在前 ↓"
                } else {
                    "从开始看 ↑"
                })
                .clicked()
            {
                self.timeline_newest = !self.timeline_newest;
            }
        });
        let stats = &self.catalog.timeline_stats;
        ui.label(
            RichText::new(format!(
                "最近扫描：整理 {} 段变化会话 · 复用 {} 段未变会话",
                stats.scanned, stats.reused
            ))
            .small()
            .color(MUTED),
        );
        ui.separator();
        let entries = timeline_entries(
            &self.catalog,
            &rows,
            &self.timeline_kind,
            self.timeline_newest,
        );
        if entries.is_empty() {
            ui.add_space(40.0);
            ui.label(
                RichText::new(if total == 0 {
                    "暂未发现关键节点"
                } else {
                    "暂无这类节点"
                })
                .size(19.0)
                .strong(),
            );
            ui.label(if total == 0 {
                "归档中出现明确需求、决定、结果或问题后，刷新会自动更新。可切换到“项目分组”阅读全部对话。"
            } else {
                "选择“全部节点”，或调整左侧会话筛选。"
            });
        }
        let mut jump = None;
        egui::ScrollArea::vertical()
            .id_salt((
                "timeline",
                identity,
                &self.timeline_kind,
                self.timeline_newest,
            ))
            .auto_shrink([false, false])
            .show_rows(ui, 180.0, entries.len(), |ui, range| {
                for row in range {
                    let (session_index, node_index) = entries[row];
                    let s = &self.catalog.sessions[session_index];
                    let node = &self.catalog.timelines[session_index][node_index];
                    let width = ui.available_width();
                    let top = ui.cursor().min;
                    let color = match node.kind.as_str() {
                        "问题" => Color32::from_rgb(170, 102, 45),
                        "修复" | "成果" => Color32::from_rgb(35, 118, 99),
                        _ => ACCENT,
                    };
                    ui.painter().line_segment(
                        [top + egui::vec2(9.0, 0.0), top + egui::vec2(9.0, 184.0)],
                        egui::Stroke::new(2.0, LINE),
                    );
                    ui.painter()
                        .circle_filled(top + egui::vec2(9.0, 24.0), 5.0, color);
                    ui.allocate_ui_with_layout(
                        egui::vec2(width, 180.0),
                        egui::Layout::left_to_right(egui::Align::Min),
                        |ui| {
                            ui.add_space(24.0);
                            egui::Frame::new()
                                .fill(PAPER)
                                .corner_radius(12)
                                .inner_margin(14)
                                .show(ui, |ui| {
                                    ui.with_layout(
                                        egui::Layout::top_down(egui::Align::Min),
                                        |ui| {
                                            ui.set_width((width - 54.0).max(100.0));
                                            ui.set_min_height(148.0);
                                            ui.spacing_mut().item_spacing.y = 8.0;
                                            ui.horizontal(|ui| {
                                                ui.label(
                                                    RichText::new(&node.kind).strong().color(color),
                                                );
                                                let date =
                                                    DateTime::parse_from_rfc3339(&node.timestamp)
                                                        .map(|d| {
                                                            d.with_timezone(&Local)
                                                                .format("%Y-%m-%d %H:%M")
                                                                .to_string()
                                                        })
                                                        .unwrap_or_else(|_| "时间未知".into());
                                                ui.label(RichText::new(date).small().color(MUTED))
                                                    .on_hover_text(&node.timestamp);
                                            });
                                            ui.add(
                                                egui::Label::new(
                                                    RichText::new(&node.title).size(17.0).strong(),
                                                )
                                                .truncate(),
                                            )
                                            .on_hover_text(&node.title);
                                            ui.add(
                                                egui::Label::new(format!(
                                                    "用户 · {}",
                                                    if node.request.is_empty() {
                                                        "该节点没有关联的用户提问"
                                                    } else {
                                                        &node.request
                                                    }
                                                ))
                                                .truncate(),
                                            )
                                            .on_hover_text(&node.request);
                                            ui.add(
                                                egui::Label::new(
                                                    RichText::new(format!(
                                                        "Agent · {}",
                                                        if node.response.is_empty() {
                                                            "尚无回复摘录"
                                                        } else {
                                                            &node.response
                                                        }
                                                    ))
                                                    .color(MUTED),
                                                )
                                                .truncate(),
                                            )
                                            .on_hover_text(&node.response);
                                            ui.horizontal(|ui| {
                                                if ui.link("查看原对话 ↗").clicked() {
                                                    jump =
                                                        Some((session_index, node.message_index));
                                                }
                                                ui.add(
                                                    egui::Label::new(
                                                        RichText::new(&s.display_title)
                                                            .small()
                                                            .color(MUTED),
                                                    )
                                                    .truncate(),
                                                )
                                                .on_hover_text(&s.display_title);
                                            });
                                        },
                                    );
                                });
                        },
                    );
                }
            });
        jump
    }

    fn image_settings(&mut self, ctx: &egui::Context) {
        let mut open = self.show_settings;
        let mut apply = false;
        let mut resize = false;
        egui::Window::new("设置")
            .open(&mut open)
            .default_width(1020.0)
            .default_height(730.0)
            .min_width(680.0)
            .max_width((ctx.screen_rect().width() - 64.0).max(480.0))
            .max_height((ctx.screen_rect().height() - 96.0).max(340.0))
            .vscroll(false)
            .show(ctx, |ui| {
                self.settings_tab = settings_layout(ui, self.settings_tab, |ui, tab| {
                    if tab == SettingsTab::About {
                        ui.heading("关于星匣 StarVault");
                        ui.label(format!("版本 {}", env!("CARGO_PKG_VERSION")));
                        ui.label("你的 AI 记忆，始终属于你。");
                        ui.add_space(12.0);
                        ui.hyperlink_to("GitHub 仓库", share::PROJECT);
                        ui.hyperlink_to("反馈问题", format!("{}/issues/new", share::PROJECT));
                        ui.add_space(8.0);
                        ui.label(RichText::new("将在浏览器打开 GitHub。反馈内容公开，请勿上传私人对话、附件或凭据。").small().color(MUTED));
                        return;
                    }
                    if tab == SettingsTab::Share {
                        if let Some(image) = self.share.show(ui) {
                            self.preview_image = Some(image);
                            self.preview_zoom = None;
                        }
                        return;
                    }
                    ui.heading("正文缩略图");
                    resize = ui
                        .add(
                            egui::Slider::new(&mut self.prefs.thumbnail_width, 160..=640)
                                .step_by(40.0)
                                .text("最大宽度"),
                        )
                        .changed();
                    ui.label(format!(
                        "最多 {} × {} · 保持比例，不裁切 · 点击图片预览原图",
                        self.prefs.thumbnail_width,
                        self.prefs.thumbnail_width * 3 / 4
                    ));
                    ui.separator();
                    ui.heading("图片文件夹");
                    ui.label("选择已迁移的 images 文件夹，所有会话将自动按图片文件名关联预览。");
                    ui.label(
                        RichText::new("图片文件名保持不变即可，不会复制或上传图片。")
                            .small()
                            .color(MUTED),
                    );
                    ui.add(
                        egui::TextEdit::singleline(&mut self.images_input)
                            .desired_width(f32::INFINITY)
                            .hint_text("留空使用备份目录下的 images"),
                    );
                    ui.horizontal(|ui| {
                        if ui.button("浏览文件夹…").clicked() {
                            if let Some(path) = rfd::FileDialog::new()
                                .set_directory(image_directory(&self.root, &self.prefs.images))
                                .pick_folder()
                            {
                                self.images_input = path.to_string_lossy().to_string();
                            }
                        }
                        if ui.button("恢复默认").clicked() {
                            self.images_input.clear();
                        }
                        let valid = image_directory(&self.root, self.images_input.trim()).is_dir();
                        if ui
                            .add_enabled(valid, egui::Button::new("应用并预览"))
                            .clicked()
                        {
                            apply = true;
                        }
                        if !valid {
                            ui.colored_label(Color32::DARK_RED, "文件夹不存在");
                        }
                    });
                    ui.label(
                        RichText::new(format!(
                            "当前：{}",
                            image_directory(&self.root, &self.prefs.images).display()
                        ))
                        .small()
                        .color(MUTED),
                    );
                });
            });
        if resize {
            for card in &mut self.cards {
                card.size.x = 0.0;
            }
            self.save_preferences();
        }
        if apply {
            self.prefs.images = if self.images_input.trim().is_empty() {
                String::new()
            } else {
                match image_directory(&self.root, self.images_input.trim()).canonicalize() {
                    Ok(path) => path.to_string_lossy().to_string(),
                    Err(error) => {
                        self.status = format!("图片目录读取失败：{error}");
                        return;
                    }
                }
            };
            self.save_preferences();
            self.render_selected(ctx);
            open = false;
        }
        self.show_settings = open;
    }

    fn image_preview(&mut self, ctx: &egui::Context) {
        let Some(image) = self.preview_image.clone() else {
            return;
        };
        let bounds = ctx.screen_rect().size();
        let area = egui::vec2(
            (bounds.x - 120.0).clamp(240.0, 1100.0),
            (bounds.y - 260.0).clamp(180.0, 800.0),
        );
        let natural_size = ctx
            .try_load_texture(&image.uri, egui::TextureOptions::LINEAR, Default::default())
            .ok()
            .and_then(|texture| texture.size());
        let fit_scale = natural_size
            .map(|size| (area.x / size.x).min(area.y / size.y).min(1.0))
            .unwrap_or(1.0);
        let mut close = false;
        let modal = egui::Modal::new(egui::Id::new("image-preview"))
            .backdrop_color(Color32::from_black_alpha(155))
            .frame(
                egui::Frame::new()
                    .fill(PAPER)
                    .corner_radius(16)
                    .inner_margin(20),
            )
            .show(ctx, |ui| {
                ui.set_width(area.x);
                ui.horizontal(|ui| {
                    ui.heading("图片预览");
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        close = ui.button("关闭  ×").clicked();
                    });
                });
                ui.horizontal(|ui| {
                    if ui
                        .selectable_label(self.preview_zoom.is_none(), "适应窗口")
                        .clicked()
                    {
                        self.preview_zoom = None;
                    }
                    if ui
                        .selectable_label(self.preview_zoom == Some(1.0), "原尺寸")
                        .clicked()
                    {
                        self.preview_zoom = Some(1.0);
                    }
                    let mut percent = self.preview_zoom.unwrap_or(fit_scale) * 100.0;
                    if ui
                        .add(
                            egui::Slider::new(&mut percent, 10.0..=400.0)
                                .suffix("%")
                                .logarithmic(true),
                        )
                        .changed()
                    {
                        self.preview_zoom = Some(percent / 100.0);
                    }
                });
                egui::ScrollArea::both()
                    .id_salt((&image.uri, self.preview_zoom.is_none()))
                    .max_height(area.y)
                    .min_scrolled_height(area.y)
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        let mut picture = egui::Image::new(&image.uri)
                            .fit_to_original_size(self.preview_zoom.unwrap_or(1.0))
                            .alt_text(&image.alt);
                        if self.preview_zoom.is_none() {
                            picture = picture.max_size(area);
                        }
                        ui.vertical_centered(|ui| {
                            ui.add(picture);
                        });
                    });
                ui.label(
                    RichText::new("原图保存在本地 · 放大后可滚动查看 · Esc 或点击遮罩关闭")
                        .small()
                        .color(MUTED),
                );
                if let Some(size) = natural_size {
                    ui.label(
                        RichText::new(format!("{} × {} 像素", size.x as u32, size.y as u32))
                            .small()
                            .color(MUTED),
                    );
                }
            });
        if close || modal.should_close() {
            self.preview_image = None;
            self.preview_zoom = None;
        }
    }

    fn local_record_path(&self, i: usize) -> Option<PathBuf> {
        let root = self.root.canonicalize().ok()?;
        let relative = &self.catalog.sessions[i].meta.local_record;
        if relative.is_empty() {
            return None;
        }
        let path = root.join(relative).canonicalize().ok()?;
        (path.starts_with(root.join("records")) && path.is_file()).then_some(path)
    }

    fn start_job(&mut self, ctx: &egui::Context, args: Vec<String>, plan: bool) {
        if self.job_receiver.is_some() {
            return;
        }
        self.job_receiver = Some(spawn_backup_job(ctx, args));
        self.job_is_plan = plan;
        self.status = if plan {
            "正在估算本地迁移空间…"
        } else {
            "正在处理本地备份，窗口仍可浏览…"
        }
        .into();
    }

    fn poll_job(&mut self, ctx: &egui::Context) {
        if let Some(result) = self.job_receiver.as_ref().and_then(|rx| rx.try_recv().ok()) {
            self.job_receiver = None;
            match result {
                Ok(value) => {
                    if self.job_is_plan {
                        self.migration_plan = Some(value);
                        self.status = "空间估算完成".into();
                    } else {
                        self.status = if value["settings_only"] == true {
                            self.migration_saved_kinds = value["kinds"]
                                .as_array()
                                .into_iter()
                                .flatten()
                                .filter_map(|v| v.as_str().map(str::to_string))
                                .collect();
                            "已保存后续备份偏好，现有附件保留。".into()
                        } else if let Some(n) = value.get("imported_chatgpt") {
                            format!("已导入 {n} 个 ChatGPT 会话，下次同步上传对话文字。")
                        } else {
                            format!(
                                "本地迁移完成：复制 {} 个文件，{} 项路径引用未找到本地文件。",
                                value["copied"], value["missing"]
                            )
                        };
                        self.render_selected(ctx);
                        self.reload(ctx);
                    }
                }
                Err(error) => self.status = format!("操作未完成：{error}"),
            }
        }
    }

    fn start_skill_job(&mut self, ctx: &egui::Context, job: SkillJob, extra: Vec<String>) {
        if self.skill_receiver.is_some() {
            return;
        }
        let mut args = vec![
            "skills".into(),
            "--archive".into(),
            self.root.to_string_lossy().to_string(),
        ];
        match job {
            SkillJob::Plan | SkillJob::Backup => {
                args.push(
                    if self.skill_defaults {
                        "--defaults"
                    } else {
                        "--no-defaults"
                    }
                    .into(),
                );
                for source in self
                    .skill_sources
                    .lines()
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                {
                    args.extend(["--source".into(), source.into()]);
                }
                if job == SkillJob::Plan {
                    args.push("--plan".into());
                    self.skill_report = None;
                } else if self.remember_skills {
                    args.push("--remember".into());
                }
            }
            SkillJob::List => args.push("--list".into()),
            SkillJob::Forget => args.push("--forget".into()),
            SkillJob::Restore => {}
        }
        args.extend(extra);
        self.skill_receiver = Some(spawn_backup_job(ctx, args));
        self.skill_job = job;
        self.skill_error = false;
        self.skill_status = match job {
            SkillJob::Plan => "正在扫描 Skill 并估算空间…",
            SkillJob::Backup => "正在备份 Skill…",
            SkillJob::List => "正在读取已备份的 Skill…",
            SkillJob::Restore => "正在安全恢复 Skill…",
            SkillJob::Forget => "正在停用当前电脑的 Skill 定时更新…",
        }
        .into();
    }

    fn poll_skill_job(&mut self) {
        let Some(result) = self
            .skill_receiver
            .as_ref()
            .and_then(|rx| rx.try_recv().ok())
        else {
            return;
        };
        self.skill_receiver = None;
        match result {
            Err(error) => {
                self.skill_error = true;
                self.skill_status = format!("Skill 操作未完成：{error}");
                if self.skill_job == SkillJob::Forget {
                    self.remember_skills = self.remembered_skills;
                }
            }
            Ok(value) => {
                self.skill_error = false;
                match self.skill_job {
                    SkillJob::Restore => {
                        self.skill_status = format!(
                            "已恢复 {} 个文件至：{}",
                            value["files"],
                            value["restored"].as_str().unwrap_or("所选目录")
                        );
                    }
                    SkillJob::Forget => {
                        self.remembered_skills = false;
                        self.remember_skills = false;
                        self.skill_status =
                            "已停用当前电脑的 Skill 定时更新，现有备份保留。".into();
                    }
                    job => {
                        if job == SkillJob::List && !self.skill_settings_loaded {
                            if let Some(defaults) = value["settings"]["defaults"].as_bool() {
                                self.skill_defaults = defaults;
                            }
                            if let Some(sources) = value["settings"]["sources"].as_array() {
                                self.skill_sources = sources
                                    .iter()
                                    .filter_map(|v| v.as_str())
                                    .collect::<Vec<_>>()
                                    .join("\n");
                            }
                            self.remembered_skills = value["remembered"].as_bool().unwrap_or(false);
                            self.remember_skills = self.remembered_skills;
                            self.skill_settings_loaded = true;
                        }
                        self.skill_report_is_plan = job == SkillJob::Plan;
                        self.skill_status = match job {
                            SkillJob::Plan => "扫描完成，可检查来源和空间后备份。".into(),
                            SkillJob::Backup => {
                                if self.remember_skills {
                                    self.remembered_skills = true;
                                }
                                format!("Skill 备份完成，本次新增 {} 个备份包。", value["copied"])
                            }
                            _ => "已读取 Skill 备份。".into(),
                        };
                        self.skill_report = Some(value);
                    }
                }
            }
        }
    }

    fn skills_window(&mut self, ctx: &egui::Context) {
        let mut open = self.show_skills;
        let mut action = None;
        let idle = self.skill_receiver.is_none();
        egui::Window::new("Skill 迁移")
            .open(&mut open)
            .default_width(720.0)
            .default_height(650.0)
            .max_width((ctx.screen_rect().width() - 64.0).max(320.0))
            .max_height((ctx.screen_rect().height() - 100.0).max(240.0))
            .vscroll(true)
            .show(ctx, |ui| {
                ui.label("本地备份与恢复 Skill · 只复制文件，不执行内容");
                ui.label(RichText::new("默认：Codex、Claude、~/.agents/skills 及本项目；不含插件缓存。").small().color(MUTED));
                ui.add_enabled_ui(idle, |ui| {
                    let mut changed = ui.checkbox(&mut self.skill_defaults, "扫描默认 Skill 目录").changed();
                    egui::CollapsingHeader::new("其他 Skill 目录（每行一个）")
                        .id_salt(("skill-extra-sources", &self.root, self.skill_settings_loaded))
                        .default_open(!self.skill_sources.trim().is_empty())
                        .show(ui, |ui| {
                            changed |= ui.add(
                                egui::TextEdit::multiline(&mut self.skill_sources)
                                    .desired_width(f32::INFINITY)
                                    .desired_rows(3)
                                    .hint_text("可粘贴 Skill 文件夹或包含多个 Skill 的目录"),
                            ).changed();
                            if ui.button("添加目录…").clicked() {
                                if let Some(path) = rfd::FileDialog::new().pick_folder() {
                                    if !self.skill_sources.is_empty() && !self.skill_sources.ends_with('\n') {
                                        self.skill_sources.push('\n');
                                    }
                                    self.skill_sources.push_str(&path.to_string_lossy());
                                    changed = true;
                                }
                            }
                        });
                    if changed && self.skill_report_is_plan {
                        self.skill_report = None;
                    }
                    if ui.checkbox(&mut self.remember_skills, "随现有定时备份更新").changed()
                        && !self.remember_skills && self.remembered_skills
                    {
                        action = Some((SkillJob::Forget, vec![]));
                    }
                    ui.label(RichText::new("勾选后在备份时保存；取消勾选即停用 Skill 定时更新。").small().color(MUTED));
                });
                ui.horizontal_wrapped(|ui| {
                    if ui.add_enabled(idle, egui::Button::new("估算空间")).clicked() {
                        action = Some((SkillJob::Plan, vec![]));
                    }
                    let fits = self.skill_report_is_plan && self.skill_report.as_ref().is_some_and(|v| {
                        v["fits"] == true && v["skills"].as_u64().unwrap_or(0) > 0
                    });
                    if ui.add_enabled(idle && fits, egui::Button::new("备份 Skill"))
                        .on_disabled_hover_text("先估算空间并确认有可备份的 Skill。")
                        .clicked()
                    {
                        action = Some((SkillJob::Backup, vec![]));
                    }
                    if ui.add_enabled(idle, egui::Button::new("查看已备份")).clicked() {
                        action = Some((SkillJob::List, vec![]));
                    }
                    if ui.button("备份目录").clicked() {
                        let directory = self.root.join("skills");
                        match fs::create_dir_all(&directory) {
                            Ok(()) => reveal(&directory),
                            Err(error) => {
                                self.skill_error = true;
                                self.skill_status = format!("无法打开 Skill 备份目录：{error}");
                            }
                        }
                    }
                    if !idle { ui.spinner(); }
                });
                if !self.skill_status.is_empty() {
                    ui.add(egui::Label::new(RichText::new(&self.skill_status)
                        .color(if self.skill_error { Color32::DARK_RED } else { MUTED })).wrap());
                }
                if let Some(report) = &self.skill_report {
                    ui.separator();
                    ui.label(RichText::new(if self.skill_report_is_plan { "扫描结果 · 尚未备份" } else { "已备份 Skill" }).strong());
                    let items = report["items"].as_array().map(Vec::as_slice).unwrap_or(&[]);
                    let files: u64 = items.iter().filter_map(|item| item["files"].as_u64()).sum();
                    ui.label(format!("{} 个 Skill · {files} 个文件", items.len()));
                    if let (Some(needed), Some(free)) = (report["estimated_bytes"].as_u64(), report["free_bytes"].as_u64()) {
                        ui.label(format!("预计新增 {:.2} MB · 可用 {:.2} GB", needed as f64 / 1048576.0, free as f64 / 1073741824.0));
                        if report["fits"] == false {
                            ui.colored_label(Color32::DARK_RED, "备份目录空间不足，请减少来源或更换备份目录。");
                        }
                    }
                    if let Some(warnings) = report["warnings"].as_array().filter(|v| !v.is_empty()) {
                        egui::CollapsingHeader::new(format!("{} 条扫描提示", warnings.len())).show(ui, |ui| {
                            for warning in warnings {
                                ui.add(egui::Label::new(warning.as_str().unwrap_or("未知提示")).wrap());
                            }
                        });
                    }
                    if let Some(items) = report["items"].as_array() {
                        if items.is_empty() { ui.label("暂无 Skill，可添加目录后重新扫描。"); }
                        for item in items {
                            let name = item["name"].as_str().unwrap_or("未命名 Skill");
                            let source = item["source"].as_str().unwrap_or("");
                            let id = item["id"].as_str().unwrap_or("");
                            ui.push_id(id, |ui| {
                                egui::Frame::group(ui.style()).show(ui, |ui| {
                                    ui.horizontal(|ui| {
                                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                            if ui.add_enabled(idle && !self.skill_report_is_plan && !id.is_empty(), egui::Button::new("恢复到…"))
                                                .on_hover_text("在所选目录下恢复为独立的 Skill 文件夹，不覆盖不同内容。")
                                                .clicked()
                                            {
                                                if let Some(path) = rfd::FileDialog::new().set_title("选择 Skill 恢复目录的父文件夹").pick_folder() {
                                                    action = Some((SkillJob::Restore, vec!["--restore".into(), id.into(), "--destination".into(), path.to_string_lossy().to_string()]));
                                                }
                                            }
                                            ui.label(RichText::new(format!("{:.1} KB", item["bytes"].as_u64().unwrap_or(0) as f64 / 1024.0)).small());
                                            ui.with_layout(egui::Layout::left_to_right(egui::Align::Center), |ui| {
                                                ui.add(egui::Label::new(RichText::new(name).strong()).truncate()).on_hover_text(name);
                                            });
                                        });
                                    });
                                    ui.add(egui::Label::new(RichText::new(format!("来源：{source}")).small().color(MUTED)).truncate()).on_hover_text(source);
                                });
                            });
                        }
                    }
                }
            });
        self.show_skills = open;
        if let Some((job, args)) = action {
            self.start_skill_job(ctx, job, args);
        }
    }

    fn resource_window(&mut self, ctx: &egui::Context) {
        let mut open = self.show_resources;
        let mut action = None;
        let mut save_policy = false;
        let previous_preview_width = self.prefs.attachment_preview_width;
        let mut attachment_action = None;
        egui::Window::new("本地附件与迁移")
            .open(&mut open).default_width(820.0).default_height(720.0)
            .max_width((ctx.screen_rect().width() - 48.0).max(320.0))
            .max_height((ctx.screen_rect().height() - 80.0).max(280.0)).vscroll(true)
            .show(ctx, |ui| {
                ui.label("筛选附件，预览文件大小，再选择需要复制的类型。");
                if let Some(i) = self.selected {
                    ui.add(egui::Label::new(RichText::new(format!("当前会话 · {}", self.catalog.sessions[i].display_title)).strong()).truncate());
                    attachment_action = attachments_view::show(ui, &self.attachment_files, &mut self.attachment_filters,
                        &self.migration_kinds, &mut self.prefs.attachment_preview_width);
                } else { ui.label("选择一个会话后预览它的附件。"); }
                ui.separator();
                ui.horizontal_wrapped(|ui| {
                    ui.label("复制范围");
                    let mut changed = ui.selectable_value(&mut self.migrate_all, false, "当前会话").changed();
                    changed |= ui.selectable_value(&mut self.migrate_all, true, "全部会话").changed();
                    if changed { self.migration_plan = None; }
                });
                let labels = attachments_view::KINDS.iter().filter(|(key, _)| self.migration_kinds.contains(*key))
                    .map(|(_, label)| *label).collect::<Vec<_>>();
                ui.label(RichText::new(format!("复制类型：{}", if labels.is_empty() { "仅会话，不额外复制附件".into() } else { labels.join("、") })).small().color(MUTED));
                egui::CollapsingHeader::new("选择类型与后续备份偏好").id_salt("attachment-policy-options").show(ui, |ui| {
                    let mut changed = false;
                    ui.horizontal_wrapped(|ui| {
                        for (label, kinds) in [("仅会话", vec![]), ("会话和图片", vec!["images"]),
                            ("所有关联文件", vec!["images", "models", "documents", "code", "logs", "other"])] {
                            let preset: BTreeSet<String> = kinds.into_iter().map(str::to_string).collect();
                            if ui.selectable_label(self.migration_kinds == preset, label).clicked() {
                                self.migration_kinds = preset; changed = true;
                            }
                        }
                    });
                    ui.horizontal_wrapped(|ui| {
                        for (key, label) in attachments_view::KINDS {
                            let mut selected = self.migration_kinds.contains(key);
                            if ui.checkbox(&mut selected, label).changed() {
                                changed = true;
                                if selected { self.migration_kinds.insert(key.into()); } else { self.migration_kinds.remove(key); }
                            }
                        }
                    });
                    if changed { self.migration_plan = None; }
                    ui.horizontal_wrapped(|ui| {
                        if ui.add_enabled(self.job_receiver.is_none(), egui::Button::new("保存后续备份偏好")).clicked() { save_policy = true; }
                        ui.label(RichText::new(if self.migration_kinds == self.migration_saved_kinds { "已与备份偏好一致" } else { "有未保存的选择" }).small().color(MUTED));
                    });
                    ui.label(RichText::new("保存偏好只影响后续备份，已有副本保留。原始会话快照可能包含内嵌资源。").small().color(MUTED));
                });
                if let Some(plan) = &self.migration_plan {
                    let needed = plan["estimated_bytes"].as_u64().unwrap_or(0) as f64 / 1048576.0;
                    let free = plan["free_bytes"].as_u64().unwrap_or(0) as f64 / 1073741824.0;
                    ui.label(format!("估算新增至多 {needed:.1} MB · 可用 {free:.1} GB · {} 项文件未找到", plan["missing"]));
                    if plan["fits"] != true { ui.colored_label(Color32::DARK_RED, "可用空间不足，请减少类型或换到更大的磁盘。"); }
                }
                ui.horizontal_wrapped(|ui| {
                    let idle = self.job_receiver.is_none();
                    if ui.add_enabled(idle, egui::Button::new("估算新增空间")).clicked() { action = Some(true); }
                    let fits = !self.migration_kinds.is_empty() && self.migration_plan.as_ref().is_some_and(|v| v["fits"] == true);
                    if ui.add_enabled(idle && fits, egui::Button::new("增量复制到备份目录")).clicked() { action = Some(false); }
                    if !idle { ui.spinner(); }
                });
                ui.label(RichText::new(&self.status).small().color(MUTED));
                if let Some(path) = self.selected.and_then(|i| self.local_record_path(i)) {
                    egui::CollapsingHeader::new("原文与关联清单").show(ui, |ui| {
                        ui.horizontal_wrapped(|ui| {
                            if ui.button("关联清单与原始元数据").clicked() { reveal(&path); }
                            if ui.button("原始对话文本").clicked() { reveal(&path.with_file_name("dialogue.jsonl")); }
                            if path.with_file_name("operations.jsonl").exists() && ui.button("已备份的命令日志").clicked() { reveal(&path.with_file_name("operations.jsonl")); }
                        });
                    });
                }
            });
        self.show_resources = open;
        if previous_preview_width != self.prefs.attachment_preview_width {
            self.save_preferences();
        }
        if let Some(action) = attachment_action {
            match action {
                attachments_view::Action::Reveal(path) => reveal(&path),
                attachments_view::Action::Preview(image) => {
                    self.preview_image = Some(image);
                    self.preview_zoom = None;
                }
            }
        }
        if save_policy {
            let kinds = if self.migration_kinds.is_empty() {
                "none".into()
            } else {
                self.migration_kinds
                    .iter()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(",")
            };
            self.start_job(
                ctx,
                vec![
                    "attachment-policy".into(),
                    "--archive".into(),
                    self.root.to_string_lossy().into(),
                    "--kinds".into(),
                    kinds,
                ],
                false,
            );
            return;
        }
        if let Some(plan) = action {
            let mut args = vec![
                "migrate".into(),
                "--archive".into(),
                self.root.to_string_lossy().to_string(),
                "--kinds".into(),
                self.migration_kinds
                    .iter()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(","),
            ];
            if !self.migrate_all {
                let Some(i) = self.selected else {
                    self.status = "请先选择一个会话".into();
                    return;
                };
                let s = &self.catalog.sessions[i];
                args.extend([
                    "--branch".into(),
                    s.branch.clone(),
                    "--session".into(),
                    s.meta.id.clone(),
                ]);
            }
            if plan {
                args.push("--plan".into());
            }
            self.start_job(ctx, args, plan);
        }
    }
}

fn project_identity(s: &Session) -> (String, String, String) {
    let (kind, key) = if let Some(id) = s
        .meta
        .project_id
        .as_deref()
        .filter(|id| !id.trim().is_empty())
    {
        ("id", id)
    } else if !s.meta.cwd.is_empty() {
        ("cwd", s.meta.cwd.as_str())
    } else {
        ("name", s.meta.project.as_str())
    };
    (s.branch.clone(), kind.into(), key.into())
}

fn project_groups(catalog: &Catalog, visible: &[usize]) -> Vec<(String, Vec<usize>)> {
    let mut groups: BTreeMap<(String, String, String), Vec<usize>> = BTreeMap::new();
    for &i in visible {
        let s = &catalog.sessions[i];
        groups.entry(project_identity(s)).or_default().push(i);
    }
    let mut groups: Vec<_> = groups
        .into_iter()
        .map(|(key, rows)| (serde_json::to_string(&key).unwrap(), rows))
        .collect();
    groups.sort_by_key(|(_, rows)| {
        let s = &catalog.sessions[rows[0]];
        (
            !s.meta.project_pinned,
            s.meta.project_order.unwrap_or(999999),
            s.meta.project.clone(),
            s.branch.clone(),
        )
    });
    groups
}

fn timeline_entries(
    catalog: &Catalog,
    rows: &[usize],
    kind: &str,
    newest: bool,
) -> Vec<(usize, usize)> {
    let mut entries: Vec<_> = rows
        .iter()
        .flat_map(|&i| {
            catalog.timelines[i]
                .iter()
                .enumerate()
                .filter(move |(_, node)| kind.is_empty() || node.kind == kind)
                .map(move |(j, _)| (i, j))
        })
        .collect();
    entries.sort_by(|&(a, x), &(b, y)| {
        let left = &catalog.timelines[a][x];
        let right = &catalog.timelines[b][y];
        let left_time = DateTime::parse_from_rfc3339(&left.timestamp).ok();
        let right_time = DateTime::parse_from_rfc3339(&right.timestamp).ok();
        match (left_time, right_time) {
            (Some(l), Some(r)) => {
                if newest {
                    r.cmp(&l)
                } else {
                    l.cmp(&r)
                }
            }
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        }
        .then_with(|| catalog.sessions[a].branch.cmp(&catalog.sessions[b].branch))
        .then_with(|| {
            catalog.sessions[a]
                .meta
                .id
                .cmp(&catalog.sessions[b].meta.id)
        })
        .then(left.message_index.cmp(&right.message_index))
        .then(x.cmp(&y))
    });
    entries
}

fn timeline_project_button(
    ui: &mut egui::Ui,
    s: &Session,
    sessions: usize,
    nodes: usize,
    selected: bool,
) -> bool {
    let (rect, response) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 60.0), egui::Sense::click());
    response.widget_info(|| {
        egui::WidgetInfo::selected(
            egui::WidgetType::SelectableLabel,
            ui.is_enabled(),
            selected,
            &s.meta.project,
        )
    });
    ui.painter().rect_filled(
        rect,
        8.0,
        if selected {
            SELECTED
        } else if response.hovered() {
            HOVER
        } else {
            Color32::TRANSPARENT
        },
    );
    let details = format!(
        "{} · {} · {nodes} 节点 / {sessions} 会话",
        s.device,
        s.meta.tool.to_uppercase()
    );
    for (text, y, size, color) in [
        (
            s.meta.project.as_str(),
            8.0,
            14.0,
            if selected { ACCENT } else { INK },
        ),
        (details.as_str(), 33.0, 11.0, MUTED),
    ] {
        let mut job = egui::text::LayoutJob::simple(
            text.into(),
            egui::FontId::proportional(size),
            color,
            (rect.width() - 24.0).max(10.0),
        );
        job.wrap.max_rows = 1;
        let galley = ui.painter().layout_job(job);
        ui.painter()
            .galley(rect.min + egui::vec2(12.0, y), galley, color);
    }
    response
        .on_hover_text(format!("{}\n{}\n{}", s.meta.project, details, s.meta.cwd))
        .clicked()
}

fn session_button(ui: &mut egui::Ui, s: &Session, selected: bool, compact: bool) -> bool {
    let (rect, response) = ui.allocate_exact_size(
        egui::vec2(ui.available_width(), SESSION_ROW_HEIGHT),
        egui::Sense::click(),
    );
    let archived = s.meta.archived == Some(true);
    let description = match s.meta.archived {
        Some(true) => format!("{} · 已归档", s.display_title),
        None => format!("{} · 归档状态未知", s.display_title),
        Some(false) => s.display_title.clone(),
    };
    response.widget_info(|| {
        egui::WidgetInfo::selected(
            egui::WidgetType::SelectableLabel,
            ui.is_enabled(),
            selected,
            &description,
        )
    });
    let fill = if selected {
        SELECTED
    } else if response.hovered() {
        HOVER
    } else {
        Color32::TRANSPARENT
    };
    ui.painter().rect_filled(rect, 7.0, fill);
    if selected {
        ui.painter().rect_filled(
            egui::Rect::from_min_size(rect.min + egui::vec2(0.0, 10.0), egui::vec2(3.0, 30.0)),
            2.0,
            ACCENT,
        );
    }
    if archived {
        let origin = egui::pos2(rect.right() - 26.0, rect.top() + 8.0);
        let stroke = egui::Stroke::new(1.2, if selected { ACCENT } else { MUTED });
        // A line-drawn archive box stays crisp without an icon font on each platform.
        ui.painter().rect_stroke(
            egui::Rect::from_min_size(origin, egui::vec2(16.0, 4.0)),
            1.0,
            stroke,
            egui::StrokeKind::Inside,
        );
        ui.painter().add(egui::Shape::line(
            vec![
                origin + egui::vec2(1.5, 4.0),
                origin + egui::vec2(1.5, 14.0),
                origin + egui::vec2(14.5, 14.0),
                origin + egui::vec2(14.5, 4.0),
            ],
            stroke,
        ));
        ui.painter().line_segment(
            [
                origin + egui::vec2(6.0, 8.0),
                origin + egui::vec2(10.0, 8.0),
            ],
            stroke,
        );
    }
    let title = format!(
        "{}{}",
        if s.meta.pinned { "★ " } else { "" },
        s.display_title
    );
    let mut job = egui::text::LayoutJob::simple(
        title,
        egui::FontId::proportional(14.0),
        if selected { ACCENT } else { INK },
        (rect.width() - if archived { 46.0 } else { 24.0 }).max(10.0),
    );
    job.wrap.max_rows = 1;
    let galley = ui.painter().layout_job(job);
    ui.painter()
        .galley(rect.min + egui::vec2(12.0, 7.0), galley, INK);
    let tool = tool_name(&s.meta.tool);
    let detail = format!(
        "{}  ·  {}{}{}",
        short_date(&s.meta.updated_at),
        tool,
        if compact {
            String::new()
        } else {
            format!("  ·  {}", s.meta.project)
        },
        if s.meta.archived.is_none() {
            "  ·  归档未知"
        } else {
            ""
        }
    );
    let mut job = egui::text::LayoutJob::simple(
        detail,
        egui::FontId::proportional(11.0),
        MUTED,
        (rect.width() - 24.0).max(10.0),
    );
    job.wrap.max_rows = 1;
    ui.painter().galley(
        rect.min + egui::vec2(12.0, 29.0),
        ui.painter().layout_job(job),
        MUTED,
    );
    response
        .on_hover_cursor(egui::CursorIcon::PointingHand)
        .on_hover_text(description)
        .clicked()
}

fn tool_name(value: &str) -> String {
    match value {
        "codex" => "Codex".to_owned(),
        "claude" => "Claude".to_owned(),
        "claude-desktop" => "Claude Desktop".to_owned(),
        "chatgpt" => "ChatGPT".to_owned(),
        "openclaw" => "OpenClaw".to_owned(),
        "workbuddy" => "WorkBuddy".to_owned(),
        "dsh" => "DeepSeek Harness".to_owned(),
        other => other.to_owned(),
    }
}

fn filter_combo(
    ui: &mut egui::Ui,
    label: &str,
    value: &mut String,
    options: BTreeSet<String>,
) -> bool {
    ui.label(RichText::new(label).small().color(MUTED));
    let mut changed = false;
    egui::ComboBox::from_id_salt(label)
        .width(ui.available_width())
        .truncate()
        .selected_text(if value.is_empty() {
            "全部".to_owned()
        } else {
            value.clone()
        })
        .show_ui(ui, |ui| {
            changed |= ui.selectable_value(value, String::new(), "全部").changed();
            for option in options.into_iter().filter(|option| !option.is_empty()) {
                let text = option.clone();
                changed |= ui.selectable_value(value, option, text).changed();
            }
        })
        .response
        .on_hover_text(if value.is_empty() {
            "全部"
        } else {
            value.as_str()
        });
    changed
}

impl eframe::App for VaultApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.poll(ctx);
        self.poll_job(ctx);
        self.poll_skill_job();
        if let Some(path) = &self.screenshot {
            ctx.request_repaint_after(Duration::from_millis(100));
            for event in ctx.input(|i| i.events.clone()) {
                if let egui::Event::Screenshot { image, .. } = event {
                    let pixels: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
                    image::save_buffer(
                        path,
                        &pixels,
                        image.width() as u32,
                        image.height() as u32,
                        image::ColorType::Rgba8,
                    )
                    .expect("save screenshot");
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                }
            }
            if !self.capture_requested && self.capture_after.is_some_and(|t| Instant::now() >= t) {
                self.capture_requested = true;
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(Default::default()));
            }
        }
        if self.last_refresh.elapsed() > Duration::from_secs(60) {
            self.reload(ctx);
        }
        ctx.request_repaint_after(Duration::from_secs(30));
        if ctx.input_mut(|input| input.consume_key(egui::Modifiers::COMMAND, egui::Key::K)) {
            ctx.memory_mut(|memory| memory.request_focus(egui::Id::new("archive-search")));
        }
        egui::TopBottomPanel::top("toolbar")
            .frame(
                egui::Frame::new()
                    .fill(PAPER)
                    .inner_margin(egui::Margin::symmetric(20, 8)),
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    brand::mark(ui, 26.0);
                    ui.label(RichText::new("星匣 · StarVault").size(14.0).strong());
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        brand::github_link(ui);
                        ui.menu_button("管理", |ui| {
                            ui.visuals_mut().widgets.inactive.bg_stroke = egui::Stroke::NONE;
                            ui.set_min_width(170.0);
                            ui.label(RichText::new("资料与设置").small().color(MUTED));
                            if ui.button("附件与迁移").clicked() {
                                self.show_resources = true;
                                ui.close_menu();
                            }
                            if ui.button("Skill 备份与恢复").clicked() {
                                self.show_skills = true;
                                if self.skill_report.is_none() && self.skill_receiver.is_none() {
                                    self.start_skill_job(ctx, SkillJob::List, vec![]);
                                }
                                ui.close_menu();
                            }
                            if ui.button("设置…").clicked() {
                                self.images_input = self.prefs.images.clone();
                                self.show_settings = true;
                                ui.close_menu();
                            }
                            if ui.button("分享软件").clicked() {
                                self.settings_tab = SettingsTab::Share;
                                self.show_settings = true;
                                ui.close_menu();
                            }
                            if ui.button("关于与反馈").clicked() {
                                self.settings_tab = SettingsTab::About;
                                self.show_settings = true;
                                ui.close_menu();
                            }
                            ui.separator();
                            if ui
                                .add_enabled(
                                    self.receiver.is_none()
                                        && self.job_receiver.is_none()
                                        && self.skill_receiver.is_none(),
                                    egui::Button::new("选择备份目录…"),
                                )
                                .clicked()
                            {
                                ui.close_menu();
                                self.choose_archive(ctx);
                            }
                        });
                        if ui
                            .add_enabled(
                                self.job_receiver.is_none(),
                                egui::Button::new("导入 ChatGPT").frame(false),
                            )
                            .clicked()
                        {
                            if let Some(path) = rfd::FileDialog::new()
                                .add_filter("ChatGPT 导出文件", &["json", "zip"])
                                .pick_file()
                            {
                                self.start_job(
                                    ctx,
                                    vec![
                                        "import-chatgpt".into(),
                                        path.to_string_lossy().to_string(),
                                        "--archive".into(),
                                        self.root.to_string_lossy().to_string(),
                                    ],
                                    false,
                                );
                            }
                        }
                        if ui
                            .add_enabled(
                                self.receiver.is_none(),
                                egui::Button::new("刷新").frame(false),
                            )
                            .clicked()
                        {
                            self.reload(ctx);
                        }
                        if self.receiver.is_some() {
                            ui.spinner();
                        }
                    });
                });
            });
        egui::TopBottomPanel::bottom("footer")
            .frame(
                egui::Frame::new()
                    .fill(Color32::WHITE)
                    .inner_margin(egui::Margin::symmetric(20, 8)),
            )
            .show(ctx, |ui| {
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(
                        RichText::new("原始会话只读 · 附件留在本地")
                            .small()
                            .color(MUTED),
                    );
                    if !self.catalog.warnings.is_empty() {
                        ui.label(
                            RichText::new(format!("{} 条读取提示", self.catalog.warnings.len()))
                                .small()
                                .color(Color32::DARK_RED),
                        )
                        .on_hover_text(self.catalog.warnings.join("\n"));
                    }
                    ui.with_layout(egui::Layout::left_to_right(egui::Align::Center), |ui| {
                        ui.add(
                            egui::Label::new(RichText::new(&self.status).small().color(MUTED))
                                .truncate(),
                        )
                        .on_hover_text(&self.status);
                    });
                });
            });
        if filters_view::show(ctx, &mut self.filters, &self.catalog) {
            self.apply_filters(ctx);
        }
        let mut clicked = None;
        egui::SidePanel::left("list")
            .default_width(264.0)
            .width_range(210.0..=(ctx.available_rect().width() - 480.0).clamp(210.0, 340.0))
            .frame(egui::Frame::new().fill(PAPER).inner_margin(14))
            .show(ctx, |ui| {
                egui::TopBottomPanel::bottom("archive-shortcuts")
                    .frame(egui::Frame::NONE)
                    .show_separator_line(false)
                    .show_inside(ui, |ui| {
                        ui.add_space(8.0);
                        ui.separator();
                        if ui
                            .add_sized(
                                [ui.available_width(), 32.0],
                                egui::Button::new("打开备份文件夹").frame(false),
                            )
                            .on_hover_text(self.root.display().to_string())
                            .clicked()
                        {
                            reveal(&self.root.join("README.md"));
                        }
                        ui.label(RichText::new("本地档案 · 每分钟更新").small().color(MUTED));
                    });
                ui.horizontal(|ui| {
                    ui.label(
                        RichText::new(if self.prefs.timeline_view {
                            "项目"
                        } else {
                            "会话"
                        })
                        .size(17.0)
                        .strong(),
                    );
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.label(
                            RichText::new(if cfg!(target_os = "macos") {
                                "⌘ K 搜索"
                            } else {
                                "Ctrl K 搜索"
                            })
                            .small()
                            .color(MUTED),
                        );
                    });
                });
                let changed = ui
                    .add(
                        egui::TextEdit::singleline(&mut self.filters.query)
                            .id(egui::Id::new("archive-search"))
                            .hint_text("搜索你的对话…")
                            .margin(egui::Margin::symmetric(10, 9))
                            .desired_width(f32::INFINITY),
                    )
                    .changed();
                ui.horizontal_wrapped(|ui| {
                    let mut view_changed = false;
                    for (projects, timeline, label) in [
                        (false, false, "最近"),
                        (true, false, "项目分组"),
                        (true, true, "时间线"),
                    ] {
                        if ui
                            .selectable_label(
                                self.prefs.projects_view == projects
                                    && self.prefs.timeline_view == timeline,
                                label,
                            )
                            .clicked()
                        {
                            self.prefs.projects_view = projects;
                            self.prefs.timeline_view = timeline;
                            view_changed = true;
                        }
                    }
                    if view_changed {
                        self.save_preferences();
                    }
                });
                if changed {
                    self.apply_filters(ctx);
                }
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    let mut tab_changed = false;
                    for (value, title) in [(1, "未归档"), (2, "已归档"), (0, "全部")] {
                        tab_changed |= ui
                            .selectable_value(&mut self.filters.archived, value, title)
                            .changed();
                    }
                    if tab_changed {
                        self.apply_filters(ctx);
                    }
                });
                ui.separator();
                ui.horizontal(|ui| {
                    ui.label(RichText::new(format!("{} 个会话", self.visible.len())).strong());
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if self.prefs.timeline_view {
                            ui.label(
                                RichText::new(format!(
                                    "{} 个项目",
                                    project_groups(&self.catalog, &self.visible).len()
                                ))
                                .small()
                                .color(MUTED),
                            );
                            return;
                        }
                        if ui
                            .button(if self.oldest {
                                "最早优先 ↑"
                            } else {
                                "最近优先 ↓"
                            })
                            .clicked()
                        {
                            self.oldest = !self.oldest;
                            self.refilter();
                        }
                    });
                });
                ui.add_space(6.0);
                if self.visible.is_empty() {
                    ui.label(if self.receiver.is_some() {
                        "正在读取会话…"
                    } else {
                        "没有匹配的会话，试试清除筛选。"
                    });
                }
                ui.spacing_mut().item_spacing.y = 2.0;
                if self.prefs.timeline_view {
                    egui::ScrollArea::vertical()
                        .id_salt("timeline-projects")
                        .show(ui, |ui| {
                            for (_, rows) in project_groups(&self.catalog, &self.visible) {
                                let first = &self.catalog.sessions[rows[0]];
                                let count: usize =
                                    rows.iter().map(|&i| self.catalog.timelines[i].len()).sum();
                                let selected = self.selected.is_some_and(|i| rows.contains(&i));
                                if timeline_project_button(ui, first, rows.len(), count, selected) {
                                    clicked = Some(rows[0]);
                                }
                            }
                        });
                } else if self.prefs.projects_view {
                    egui::ScrollArea::vertical()
                        .id_salt("project-list")
                        .show(ui, |ui| {
                            let groups = project_groups(&self.catalog, &self.visible);
                            for (key, rows) in groups {
                                let first = &self.catalog.sessions[rows[0]];
                                let label = format!(
                                    "{}{} · {}",
                                    if first.meta.project_pinned {
                                        "★ "
                                    } else {
                                        ""
                                    },
                                    first.meta.project,
                                    rows.len()
                                );
                                let selected_group =
                                    self.selected.is_some_and(|i| rows.contains(&i));
                                let title = egui::WidgetText::from(
                                    RichText::new(&label).strong().color(INK),
                                )
                                .into_galley(
                                    ui,
                                    Some(egui::TextWrapMode::Truncate),
                                    (ui.available_width()
                                        - ui.spacing().indent
                                        - ui.spacing().button_padding.x)
                                        .max(0.0),
                                    egui::TextStyle::Button,
                                );
                                egui::CollapsingHeader::new(title)
                                    .id_salt(key)
                                    .default_open(selected_group)
                                    .show(ui, |ui| {
                                        ui.label(
                                            RichText::new(format!(
                                                "{} · {}",
                                                first.device,
                                                first.meta.tool.to_uppercase()
                                            ))
                                            .small()
                                            .color(MUTED),
                                        );
                                        for i in rows {
                                            if session_button(
                                                ui,
                                                &self.catalog.sessions[i],
                                                self.selected == Some(i),
                                                true,
                                            ) {
                                                clicked = Some(i);
                                            }
                                        }
                                    })
                                    .header_response
                                    .on_hover_text(label);
                            }
                        });
                } else {
                    egui::ScrollArea::vertical()
                        .id_salt("session-list")
                        .show_rows(ui, SESSION_ROW_HEIGHT, self.visible.len(), |ui, range| {
                            for row in range {
                                let i = self.visible[row];
                                if session_button(
                                    ui,
                                    &self.catalog.sessions[i],
                                    self.selected == Some(i),
                                    false,
                                ) {
                                    clicked = Some(i);
                                }
                            }
                        });
                }
            });
        if let Some(i) = clicked {
            self.selected = Some(i);
            self.render_selected(ctx);
        }
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(Color32::WHITE).inner_margin(24))
            .show(ctx, |ui| {
                let Some(i) = self.selected else {
                    ui.add_space((ui.available_height() * 0.20).max(32.0));
                    ui.vertical_centered(|ui| {
                        brand::mark(ui, 72.0);
                        ui.add_space(16.0);
                        if self.receiver.is_some() {
                            ui.heading("正在打开你的星匣");
                            ui.spinner();
                        } else if !self.catalog.sessions.is_empty() {
                            ui.heading("没有找到匹配的会话");
                            ui.label(
                                RichText::new("换个关键词，或清除筛选后再看看。").color(MUTED),
                            );
                            ui.add_space(12.0);
                            if ui.button("清除筛选").clicked() {
                                self.filters = Filters::default();
                                self.apply_filters(ctx);
                            }
                        } else {
                            ui.heading("把灵感，收藏进星匣");
                            ui.label(
                                RichText::new("选择备份目录，开始阅读你的 AI 对话。").color(MUTED),
                            );
                            ui.add_space(12.0);
                            if ui
                                .add_enabled(
                                    self.job_receiver.is_none() && self.skill_receiver.is_none(),
                                    egui::Button::new("选择备份目录…"),
                                )
                                .clicked()
                            {
                                self.choose_archive(ctx);
                            }
                        }
                    });
                    return;
                };
                if self.prefs.timeline_view {
                    if let Some((session, message)) = self.project_timeline(ui, i) {
                        self.selected = Some(session);
                        self.prefs.timeline_view = false;
                        self.render_selected(ctx);
                        self.focus_message = Some(message);
                        self.scroll_target = Some(message);
                        self.save_preferences();
                    }
                    return;
                }
                let s = &self.catalog.sessions[i];
                ui.add(
                    egui::Label::new(RichText::new(&s.display_title).size(19.0).strong())
                        .truncate(),
                )
                .on_hover_text(&s.display_title);
                let location = format!(
                    "{}  /  {}  /  {}",
                    s.device,
                    tool_name(&s.meta.tool),
                    s.meta.project
                );
                ui.add(egui::Label::new(RichText::new(&location).small().color(MUTED)).truncate())
                    .on_hover_text(location);
                let mut rerender = false;
                let mut open_timeline = false;
                ui.horizontal_wrapped(|ui| {
                    if ui
                        .add(egui::Button::new(if self.focus_message.is_some() {
                            "← 返回项目时间线"
                        } else {
                            "项目时间线"
                        }).frame(false))
                        .clicked()
                    {
                        self.prefs.timeline_view = true;
                        self.prefs.projects_view = true;
                        open_timeline = true;
                    }
                    rerender |= ui
                        .selectable_value(&mut self.prefs.text_only, false, "图文")
                        .changed();
                    rerender |= ui
                        .selectable_value(&mut self.prefs.text_only, true, "仅文字")
                        .changed();
                    if ui.add(egui::Button::new("附件与活动").frame(false))
                        .on_hover_text(format!("{} 张图片 · {} 条工具活动", s.meta.assets.len(), s.meta.operations))
                        .clicked() {
                        self.show_resources = true;
                    }
                    ui.menu_button("更多", |ui| {
                        ui.visuals_mut().widgets.inactive.bg_stroke = egui::Stroke::NONE;
                        if ui.button("复制完整对话").clicked() {
                            ctx.copy_text(s.markdown.clone());
                            ui.close_menu();
                        }
                        if ui.button("定位会话文件").clicked() {
                            reveal(&s.path);
                            ui.close_menu();
                        }
                        if ui.selectable_label(self.show_details, "会话信息").clicked() {
                            self.show_details = !self.show_details;
                            ui.close_menu();
                        }
                    });
                    if !s.meta.assets.is_empty() {
                        ui.label(
                            RichText::new(format!("{} 张图片 · {} 条工具活动", s.meta.assets.len(), s.meta.operations))
                                .small()
                                .color(MUTED),
                        );
                    }
                });
                let missing = self.missing_images;
                if missing > 0 {
                    if ui
                        .link(format!("{missing} 张图片未连接 · 设置图片文件夹"))
                        .clicked()
                    {
                        self.images_input = self.prefs.images.clone();
                        self.settings_tab = SettingsTab::Images;
                        self.show_settings = true;
                    }
                }
                if self.show_details {
                    if s.display_title != s.meta.title {
                        ui.add(
                            egui::Label::new(
                                RichText::new(format!("原始标题：{}", s.meta.title))
                                    .small()
                                    .color(MUTED),
                            )
                            .wrap(),
                        );
                    }
                    ui.label(
                        RichText::new(format!(
                            "来源：{} · {}\n模型：{}\n标签：{}",
                            s.meta.origin_kind,
                            s.meta.originator,
                            s.meta.models.join(", "),
                            s.meta.labels.join(" · ")
                        ))
                        .small()
                        .color(MUTED),
                    );
                    ui.label(
                        RichText::new(format!(
                            "会话 ID：{}\n目录：{}\n分支：{}\n缺失的原始图片引用：{}",
                            s.meta.id,
                            s.meta.cwd,
                            s.branch,
                            s.meta.unavailable_local_images.len()
                        ))
                        .small()
                        .color(MUTED),
                    );
                }
                if s.meta.tool == "chatgpt" {
                    ui.label(
                        RichText::new(match s.meta.coverage.as_str() {
                            "official_export" => "ChatGPT 导出文件 · 原始消息分支保存在本地原文中",
                            "partial_snapshot" => {
                                "ChatGPT 部分快照 · 部分内容未读全，可导入完整导出文件补齐"
                            }
                            _ => "ChatGPT 应用快照 · 云端更新需再次导入；不代表账户全部历史",
                        })
                        .small()
                        .color(MUTED),
                    );
                }
                if s.meta.coverage == "partial_cache" {
                    ui.label(
                        RichText::new("Claude Code Projects · 本地缓存，可能不完整；已备份消息持续保留，附件仅保留现有引用")
                            .small()
                            .color(MUTED),
                    );
                }
                ui.separator();
                if open_timeline {
                    self.save_preferences();
                    return;
                }
                if rerender {
                    self.render_selected(ctx);
                    self.save_preferences();
                }
                ui.push_id(format!("conversation-{i}-{}", self.prefs.text_only), |ui| {
                    if let Some(image) = show_message_cards(
                        ui,
                        &mut self.cards,
                        &mut self.cache,
                        self.prefs.thumbnail_width,
                        self.focus_message,
                        &mut self.scroll_target,
                    ) {
                        self.preview_image = Some(image);
                        self.preview_zoom = None;
                    }
                });
            });
        if self.show_settings {
            self.image_settings(ctx);
        }
        if self.show_resources {
            self.resource_window(ctx);
        }
        if self.show_skills {
            self.skills_window(ctx);
        }
        self.image_preview(ctx);
    }

    fn clear_color(&self, _: &egui::Visuals) -> [f32; 4] {
        [246.0 / 255.0, 248.0 / 255.0, 253.0 / 255.0, 1.0]
    }
}

fn main() -> eframe::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let prefs_path = app_home().join("viewer.json");
    let mut prefs: Preferences = fs::read_to_string(prefs_path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    if prefs.font_size < 12.0 {
        prefs.font_size = 15.0;
    }
    if !(160..=640).contains(&prefs.thumbnail_width) {
        prefs.thumbnail_width = default_thumbnail_width();
    }
    if !(64..=240).contains(&prefs.attachment_preview_width) {
        prefs.attachment_preview_width = default_attachment_preview_width();
    }
    if let Some(value) = args.windows(2).find(|a| a[0] == "--images") {
        prefs.images = value[1].clone();
    }
    if args.iter().any(|a| a == "--projects") {
        prefs.projects_view = true;
        prefs.timeline_view = false;
    }
    if args.iter().any(|a| a == "--timeline") {
        prefs.projects_view = true;
        prefs.timeline_view = true;
    }
    let root = args
        .windows(2)
        .find(|a| a[0] == "--archive")
        .map(|a| PathBuf::from(&a[1]))
        .unwrap_or_else(|| {
            if prefs.archive.is_empty() {
                configured_archive()
            } else {
                PathBuf::from(&prefs.archive)
            }
        });
    if args.iter().any(|a| a == "--check") {
        match load_catalog(&root) {
            Ok(catalog) => {
                println!(
                    "{} sessions, {} warnings",
                    catalog.sessions.len(),
                    catalog.warnings.len()
                );
            }
            Err(error) => {
                eprintln!("{error}");
                std::process::exit(1);
            }
        }
        return Ok(());
    }
    let select = args
        .windows(2)
        .find(|a| a[0] == "--select")
        .map(|a| a[1].clone());
    let screenshot = args
        .windows(2)
        .find(|a| a[0] == "--screenshot")
        .map(|a| PathBuf::from(&a[1]));
    let filters = Filters {
        tool: args
            .windows(2)
            .find(|a| a[0] == "--source")
            .map(|a| a[1].clone())
            .unwrap_or_default(),
        archived: if args.iter().any(|a| a == "--archived") {
            2
        } else {
            0
        },
        ..Default::default()
    };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_icon({
                let image =
                    image::load_from_memory(include_bytes!("../../assets/starvault-256.png"))
                        .expect("bundled StarVault icon")
                        .to_rgba8();
                egui::IconData {
                    width: image.width(),
                    height: image.height(),
                    rgba: image.into_raw(),
                }
            })
            .with_inner_size([1320.0, 860.0])
            .with_min_inner_size([980.0, 640.0]),
        ..Default::default()
    };
    eframe::run_native(
        "星匣 · StarVault",
        options,
        Box::new(move |cc| {
            let mut app = VaultApp::new(cc, root, prefs, select, screenshot, filters);
            app.show_settings = args.iter().any(|a| a == "--share");
            if app.show_settings {
                app.settings_tab = SettingsTab::Share;
            }
            app.show_resources = args.iter().any(|a| a == "--resources");
            app.show_skills = args.iter().any(|a| a == "--skills");
            if app.show_skills {
                app.start_skill_job(&cc.egui_ctx, SkillJob::List, vec![]);
            }
            app.preview_first = args.iter().any(|a| a == "--preview-image");
            Ok(Box::new(app))
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn skill_package_indexes_are_not_conversation_branches() {
        let root = std::env::temp_dir().join(format!("vault-skill-index-{}", std::process::id()));
        fs::create_dir_all(root.join("skills")).unwrap();
        fs::create_dir_all(root.join("machine-codex")).unwrap();
        fs::write(
            root.join("skills/index.json"),
            r#"{"format_version":1,"items":[]}"#,
        )
        .unwrap();
        fs::write(root.join("machine-codex/index.json"), r#"{"sessions":{}}"#).unwrap();
        let catalog = load_catalog(&root).unwrap();
        fs::remove_dir_all(root).unwrap();
        assert!(catalog.warnings.is_empty());
        assert!(catalog.sessions.is_empty());
    }

    #[test]
    fn display_titles_use_requests_without_overwriting_source_titles() {
        let source = "# <environment_context>\n\n## 用户 · \n<environment_context><cwd>/project</cwd></environment_context>\n\n## 用户 · \n<external_codex_apps_open_page>{\"page_id\":null}</external_codex_apps_open_page>\n\n## 用户 · \n# Files mentioned by the user:\n\n## shot.png: /tmp/shot.png\nImage attachment: true\n\n## My request:\n继续优化前端\n<image name=shot path=/tmp/shot.png>\n</image>\n\n![图](../../../images/shot.png)";
        assert_eq!(
            conversation_title("<environment_context>", source),
            "继续优化前端"
        );
        assert_eq!(conversation_title("自定义标题", source), "自定义标题");
        assert_eq!(
            conversation_title("# AGENTS.md instructions for /project", source),
            "继续优化前端"
        );
        assert!(source.starts_with("# <environment_context>"));
        let mixed = "## 用户 · \n<environment_context>cwd</environment_context>\n请解释这些标签";
        assert_eq!(
            conversation_title("<environment_context>", mixed),
            "请解释这些标签"
        );
        assert!(message_cards(mixed)[0].context.is_none());
        assert_eq!(
            conversation_title(
                "",
                "## 用户 · \n<environment_context>cwd</environment_context>"
            ),
            "未命名会话"
        );
        assert_eq!(
            conversation_title(
                "",
                "## 用户 · \n<environment_context>unfinished\n## 助手 · \n回答"
            ),
            "未命名会话"
        );
        let code = "## 用户 · \n解释这个示例\n```xml\n<environment_context>example</environment_context>\n## 用户 · fake\n```";
        assert_eq!(
            conversation_title("<environment_context>", code),
            "解释这个示例"
        );
        assert_eq!(message_cards(code).len(), 1);
    }

    #[test]
    fn context_summaries_keep_raw_markup_and_ignore_instruction_examples() {
        let environment = "<environment_context>\n<cwd>/项目/hello world</cwd>\n<shell>zsh</shell>\n<current_date>2026-10-09</current_date>\n<timezone>Asia/Shanghai</timezone>\n<filesystem><workspace_roots><root>/one</root><root>/two</root></workspace_roots></filesystem>\n<unknown>额外字段</unknown>\n</environment_context>";
        let fields = context_fields(environment);
        assert_eq!(
            fields,
            vec![
                ("工作目录", "/项目/hello world".into()),
                ("终端", "zsh".into()),
                ("日期", "2026-10-09".into()),
                ("时区", "Asia/Shanghai".into()),
                ("工作区", "/one\n/two".into())
            ]
        );
        let instructions = "# AGENTS.md instructions for /project\n<INSTRUCTIONS>示例：<environment_context><cwd>/example</cwd></environment_context>\n![图](https://example.com/a.png)\n</INSTRUCTIONS>\n";
        assert!(context_fields(instructions).is_empty());
        let combined = format!("{instructions}{environment}");
        assert_eq!(context_fields(&combined), fields);
        let cards = message_cards(&format!("## 用户 · \n{combined}"));
        assert_eq!(cards[0].markdown, combined);
        assert!(cards[0].context.is_some());
        assert_eq!(
            context_fields(
                "<external_codex_apps_open_page>{\"page_id\":null}</external_codex_apps_open_page>"
            ),
            vec![("关联页面", "无".into())]
        );
        assert!(context_fields(
            "<external_codex_apps_open_page>bad json</external_codex_apps_open_page>"
        )
        .is_empty());
    }

    #[test]
    fn auxiliary_folding_never_hides_a_user_request() {
        assert_eq!(
            context_only("<environment_context>cwd</environment_context>"),
            Some("环境上下文")
        );
        let instructions = "# AGENTS.md instructions for /project\n<INSTRUCTIONS>rules</INSTRUCTIONS>\n<environment_context>cwd</environment_context>";
        assert_eq!(context_only(instructions), Some("项目说明与上下文"));
        assert_eq!(context_only(&format!("{instructions}\n请修复布局")), None);
        assert_eq!(context_only("请检查 AGENTS.md"), None);
        assert_eq!(context_only("<environment_context>unfinished"), None);
        assert_eq!(
            context_only("# AGENTS.md instructions for /project\n请解释这个标签 </INSTRUCTIONS>"),
            None
        );
        let cards = message_cards(&format!(
            "## 用户 · \n{instructions}\n## 助手 · \n{instructions}"
        ));
        assert!(cards[0].context.is_some());
        assert!(cards[1].context.is_none());
        let original = "# Files mentioned by the user:\n\n## example.png: /tmp/example.png\nImage attachment: true\n\n## My request:\n\n请优化图片\n<image name=example path=/tmp/example.png>\n</image>\n\n![图](../../../images/hash.png)";
        let (request, context) = separate_attachment_context(original);
        assert!(request.contains("请优化图片"));
        assert!(request.contains("![图](../../../images/hash.png)"));
        assert!(!request.contains("<image name="));
        assert!(context.contains("/tmp/example.png"));
        assert_eq!(
            separate_attachment_context("请解释 <image name=example></image>").0,
            "请解释 <image name=example></image>"
        );
    }

    #[test]
    fn tall_thumbnail_is_bounded_and_click_opens_original() {
        let ctx = egui::Context::default();
        let uri = "vault-file:///test/tall.png";
        let mut bytes = std::io::Cursor::new(Vec::new());
        image::DynamicImage::ImageRgba8(image::RgbaImage::new(400, 1600))
            .write_to(&mut bytes, image::ImageFormat::Png)
            .unwrap();
        ctx.include_bytes(uri, bytes.into_inner());
        let mut cards = message_cards(&format!("## 用户 · \n\n![截图]({uri})\n"));
        (cards[0].markdown, cards[0].images) = thumbnail_markdown(&cards[0].markdown);
        let mut cache = CommonMarkCache::default();
        let mut run = |events: Vec<egui::Event>| {
            let mut clicked = None;
            let output = ctx.run(
                egui::RawInput {
                    screen_rect: Some(egui::Rect::from_min_size(
                        egui::Pos2::ZERO,
                        egui::vec2(600.0, 700.0),
                    )),
                    events,
                    ..Default::default()
                },
                |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| {
                        if let Some(image) =
                            show_message_cards(ui, &mut cards, &mut cache, 320, None, &mut None)
                        {
                            clicked = Some(image.uri);
                        }
                    });
                },
            );
            (output, clicked)
        };
        let mut image_rect = None;
        for _ in 0..10 {
            let (output, _) = run(vec![]);
            for shape in ctx.tessellate(output.shapes, output.pixels_per_point) {
                if let egui::epaint::Primitive::Mesh(mesh) = shape.primitive {
                    if mesh.texture_id != egui::TextureId::default() {
                        image_rect = Some(mesh.calc_bounds());
                    }
                }
            }
            if image_rect.is_some() {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        let rect = image_rect.expect("thumbnail should render");
        assert!((rect.width() - 60.0).abs() <= 1.1, "{rect:?}");
        assert!((rect.height() - 240.0).abs() <= 1.1, "{rect:?}");
        let pos = rect.center();
        run(vec![
            egui::Event::PointerMoved(pos),
            egui::Event::PointerButton {
                pos,
                button: egui::PointerButton::Primary,
                pressed: true,
                modifiers: Default::default(),
            },
        ]);
        let (_, clicked) = run(vec![egui::Event::PointerButton {
            pos,
            button: egui::PointerButton::Primary,
            pressed: false,
            modifiers: Default::default(),
        }]);
        assert_eq!(clicked.as_deref(), Some(uri));
    }

    #[test]
    fn conversation_scroll_survives_centering_and_resize() {
        let ctx = egui::Context::default();
        let markdown = (0..40).map(|i| format!(
            "## 用户 · 2026-10-25T10:00:00Z\n\nQuestion {i}.\n\n## 助手 · 2026-10-25T10:00:00Z\n\nMessage {i}: a paragraph to read.\n\n```rust\nlet value = {i};\n```\n\n| Item | Value |\n| --- | --- |\n| example | {i} |\n\n"
        )).collect::<String>();
        let mut cards = message_cards(&markdown);
        let mut cache = CommonMarkCache::default();
        let mut frame = |width, delta| {
            let mut offset = 0.0;
            let _ = ctx.run(
                egui::RawInput {
                    screen_rect: Some(egui::Rect::from_min_size(
                        egui::Pos2::ZERO,
                        egui::vec2(width, 640.0),
                    )),
                    events: vec![
                        egui::Event::PointerMoved(egui::pos2(400.0, 300.0)),
                        egui::Event::MouseWheel {
                            unit: egui::MouseWheelUnit::Point,
                            delta: egui::vec2(0.0, delta),
                            modifiers: Default::default(),
                        },
                    ],
                    ..Default::default()
                },
                |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| {
                        let id = ui.make_persistent_id(egui::Id::new("message-cards"));
                        show_message_cards(ui, &mut cards, &mut cache, 320, None, &mut None);
                        offset = egui::scroll_area::State::load(ctx, id).unwrap().offset.y;
                    });
                },
            );
            offset
        };
        for _ in 0..3 {
            frame(1000.0, 0.0);
        }
        for _ in 0..6 {
            frame(1000.0, -120.0);
        }
        assert!(
            frame(1000.0, 0.0) > 200.0,
            "long conversation should scroll down"
        );
        assert!(
            frame(640.0, 0.0) > 100.0,
            "resizing must not reset scroll position"
        );
        for _ in 0..20 {
            frame(640.0, 120.0);
        }
        assert!(
            frame(640.0, 0.0) < 1.0,
            "conversation should return to the start"
        );
    }

    #[test]
    fn thumbnails_preserve_code_examples_and_old_preferences() {
        let source = "前文\n\n### 图片 · 2026-10-08T14:19:18Z\n\n![纵图](vault-file:///archive/a.png)\n\n后文\n\n```markdown\n![示例](vault-file:///code.png)\n```\n\n`![inline](vault-file:///code.png)`\n\n![远程](https://example.com/a.png)\n";
        let (rendered, images) = thumbnail_markdown(source);
        assert_eq!(images.len(), 1);
        assert_eq!(images[0].alt, "纵图");
        assert_eq!(images[0].uri, "vault-file:///archive/a.png");
        assert!(rendered.contains("<vault-thumbnail-0>"));
        assert!(!rendered.contains("### 图片 ·"));
        assert!(rendered.contains("后文\n\n```markdown\n![示例]"));
        assert!(rendered.contains("`![inline](vault-file:///code.png)`"));
        assert!(rendered.contains("![远程](https://example.com/a.png)"));
        let mut prefs: Preferences = serde_json::from_str("{}").unwrap();
        assert_eq!(prefs.thumbnail_width, 320);
        prefs.thumbnail_width = 480;
        let saved = serde_json::to_string(&prefs).unwrap();
        assert_eq!(
            serde_json::from_str::<Preferences>(&saved)
                .unwrap()
                .thumbnail_width,
            480
        );
    }

    #[test]
    fn message_cards_preserve_markdown_and_ignore_role_examples_in_code() {
        let user = "\n问题\n```markdown\n## 助手 · example\n```\n\n";
        let assistant = "\n## 普通标题\n答复\n~~~text\n## 用户 · example\n~~~\n![图](../../../images/sample.png)\n";
        let cards = message_cards(&format!(
            "## 用户 · 2026-10-08T08:00:00Z\n{user}## assistant · \n{assistant}"
        ));
        assert_eq!(cards.len(), 2);
        assert_eq!(cards[0].role, "用户");
        assert_eq!(cards[0].markdown, user);
        assert_eq!(cards[1].role, "助手");
        assert_eq!(cards[1].markdown, assistant);
        assert_eq!(
            message_cards("### 图片\n![图](local.png)")[0].role,
            "原文 / 附件"
        );
    }

    #[test]
    fn filters_cover_content_tool_project_and_archive() {
        let session = Session {
            display_title: String::new(),
            meta: SessionMeta {
                tool: "codex".into(),
                project: "游戏".into(),
                models: vec!["claude-test".into()],
                labels: vec!["tags:原始标签".into()],
                archived: Some(true),
                ..Default::default()
            },
            branch: "mac-codex".into(),
            device: "mac".into(),
            path: PathBuf::new(),
            markdown: String::new(),
            search: "地图设计 hello".into(),
        };
        let mut filters = Filters {
            query: "HELLO 地图".into(),
            archived: 2,
            ..Default::default()
        };
        assert!(filters.matches(&session));
        filters.model = "claude-test".into();
        filters.label = "tags:原始标签".into();
        assert!(filters.matches(&session));
        filters.model = "other-model".into();
        assert!(!filters.matches(&session));
        filters.model.clear();
        filters.tool = "claude".into();
        assert!(!filters.matches(&session));
        filters.tool.clear();
        filters.archived = 1;
        assert!(!filters.matches(&session));
    }
    #[test]
    fn every_archive_source_has_a_readable_label() {
        assert_eq!(tool_name("codex"), "Codex");
        assert_eq!(tool_name("claude"), "Claude");
        assert_eq!(tool_name("claude-desktop"), "Claude Desktop");
        assert_eq!(tool_name("chatgpt"), "ChatGPT");
        assert_eq!(tool_name("openclaw"), "OpenClaw");
        assert_eq!(tool_name("workbuddy"), "WorkBuddy");
        assert_eq!(tool_name("dsh"), "DeepSeek Harness");
        assert_eq!(tool_name("custom-source"), "custom-source");
    }
    #[test]
    fn unsafe_and_remote_images_are_not_loaded() {
        let source = "![a](https://example.com/secret.png) ![b](file:///private/secret.png) ![c](../../../images/../../secret.png)";
        let result = prepare_markdown(source, Path::new("/tmp"), false);
        assert!(!result.contains("!["));
        assert!(!result.contains("file:"));
        assert!(!result.contains("https:"));
        assert_eq!(
            prepare_markdown("![图](anything)", Path::new("/tmp"), true),
            "〔图片：图〕"
        );
    }

    #[test]
    fn moved_image_folder_loads_with_spaces_unicode_and_percent() {
        use egui::load::{BytesLoader, BytesPoll};
        let root =
            std::env::temp_dir().join(format!("vault-{}-迁移 图片 (100%)", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        image::RgbaImage::new(2, 2)
            .save(root.join("sample.png"))
            .unwrap();
        let markdown = prepare_markdown("![图](../../../images/sample.png)", &root, false);
        let uri = IMAGE_LINK.captures(&markdown).unwrap()[2].to_string();
        assert!(uri.starts_with("vault-file://"));
        let loader = LocalImageUrls::default();
        let ctx = egui::Context::default();
        let mut loaded = false;
        for _ in 0..100 {
            if let BytesPoll::Ready { bytes, .. } = loader.load(&ctx, &uri).unwrap() {
                assert_eq!(image::load_from_memory(bytes.as_ref()).unwrap().width(), 2);
                loaded = true;
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        assert!(loaded);
        assert!(loader.byte_size() > 0);
        loader.forget_all();
        assert_eq!(loader.byte_size(), 0);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn groups_use_project_identity_and_keep_archived_and_cli_sessions() {
        let make = |id: Option<&str>, cwd: &str, archived| Session {
            display_title: String::new(),
            meta: SessionMeta {
                project: "同名项目".into(),
                project_id: id.map(str::to_owned),
                cwd: cwd.into(),
                archived,
                ..Default::default()
            },
            branch: "mac-codex".into(),
            device: "mac".into(),
            path: PathBuf::new(),
            markdown: String::new(),
            search: String::new(),
        };
        let catalog = Catalog {
            sessions: vec![
                make(Some("project-a"), "/a", Some(false)),
                make(Some("project-a"), "/a/worktree", Some(true)),
                make(None, "/b", None),
            ],
            ..Default::default()
        };
        let groups = project_groups(&catalog, &[0, 1, 2]);
        assert_eq!(groups.len(), 2);
        assert!(groups.iter().any(|(_, rows)| rows == &[0, 1]));
        let active = Filters {
            archived: 1,
            ..Default::default()
        };
        let archived = Filters {
            archived: 2,
            ..Default::default()
        };
        assert!(active.matches(&catalog.sessions[2]));
        assert!(!active.matches(&catalog.sessions[1]));
        assert!(archived.matches(&catalog.sessions[1]));
        assert!(!archived.matches(&catalog.sessions[2]));
    }
}
