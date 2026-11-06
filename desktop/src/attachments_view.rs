//! Attachment browsing is read-only; copy choices stay in the migration panel.
use super::{egui, PreviewImage, RichText, INK, MUTED, PAPER};
use serde_json::Value;
use std::{
    collections::BTreeSet,
    fs,
    path::{Component, Path, PathBuf},
};

pub const KINDS: [(&str, &str); 6] = [
    ("images", "图片"),
    ("models", "GLB / Blender"),
    ("documents", "文档"),
    ("code", "代码"),
    ("logs", "命令 / 日志"),
    ("other", "其他附件"),
];

#[derive(Clone)]
pub struct Attachment {
    pub name: String,
    pub source: String,
    pub kind: String,
    pub bytes: Option<u64>,
    pub copy: Option<PathBuf>,
    pub source_exists: bool,
    pub current: bool,
}
impl Attachment {
    fn status(&self) -> &'static str {
        if self.current {
            "已备份"
        } else if self.source_exists {
            if self.copy.is_some() {
                "待更新"
            } else {
                "待复制"
            }
        } else if self.copy.is_some() {
            "仅有副本"
        } else {
            "未找到"
        }
    }
}

// Accept only regular archive objects; never preview remote URLs or arbitrary source paths.
fn archive_copy(root: &Path, relative: &str, images: Option<&Path>) -> Option<PathBuf> {
    if relative.contains('\\') {
        return None;
    }
    let parts: Vec<_> = Path::new(relative).components().collect();
    if parts.len() != 2
        || !parts.iter().all(|p| matches!(p, Component::Normal(_)))
        || !matches!(
            parts[0].as_os_str().to_str(),
            Some("images" | "attachments")
        )
    {
        return None;
    }
    let configured = parts[0].as_os_str() == "images" && images.is_some();
    let root = if configured { images? } else { root }
        .canonicalize()
        .ok()?;
    let mut path = root.clone();
    for part in parts.into_iter().skip(usize::from(configured)) {
        path.push(part.as_os_str());
        let meta = fs::symlink_metadata(&path).ok()?;
        if meta.is_symlink() {
            return None;
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if meta.file_attributes() & 0x400 != 0 {
                return None;
            }
        }
    }
    let path = path.canonicalize().ok()?;
    (path.starts_with(root) && path.is_file()).then_some(path)
}

pub fn entries(root: &Path, manifest: &Value, images: Option<&Path>) -> Vec<Attachment> {
    let mut files: Vec<Attachment> = manifest["files"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|file| {
            let source = file["source"].as_str()?.to_owned();
            let copy = file["copy"]
                .as_str()
                .and_then(|p| archive_copy(root, p, images));
            let exists = file["exists"].as_bool().unwrap_or(false);
            let current = copy.is_some()
                && (!exists
                    || (file["size"].as_u64().is_some()
                        && file["size"] == file["copied_size"]
                        && file["mtime_ns"].as_u64().is_some()
                        && file["mtime_ns"] == file["copied_mtime_ns"]));
            let bytes = if exists {
                file["size"].as_u64()
            } else {
                copy.as_ref()
                    .and_then(|p| p.metadata().ok())
                    .map(|m| m.len())
            };
            Some(Attachment {
                name: file["name"].as_str().unwrap_or(&source).to_owned(),
                kind: file["kind"].as_str().unwrap_or("other").into(),
                source,
                bytes,
                copy,
                source_exists: exists,
                current,
            })
        })
        .collect();
    let mut copies: BTreeSet<PathBuf> = files.iter().filter_map(|file| file.copy.clone()).collect();
    for name in manifest["metadata"]["assets"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
    {
        let Some((hash, extension)) = name.split_once('.') else {
            continue;
        };
        if hash.len() != 64
            || !hash.bytes().all(|b| b.is_ascii_hexdigit())
            || !matches!(extension, "png" | "jpg" | "gif" | "webp")
        {
            continue;
        }
        let relative = format!("images/{name}");
        let copy = archive_copy(root, &relative, images);
        if copy
            .as_ref()
            .is_some_and(|path| !copies.insert(path.clone()))
        {
            continue;
        }
        let bytes = copy
            .as_ref()
            .and_then(|p| p.metadata().ok())
            .map(|m| m.len());
        files.push(Attachment {
            name: format!("对话图片 · {}.{extension}", &hash[..8]),
            source: relative,
            kind: "images".into(),
            bytes,
            source_exists: false,
            current: copy.is_some(),
            copy,
        });
    }
    files
}

pub struct Filters {
    query: String,
    kind: String,
    status: u8,
    min_bytes: u64,
    largest: bool,
    previews: bool,
}
impl Default for Filters {
    fn default() -> Self {
        Self {
            query: String::new(),
            kind: String::new(),
            status: 0,
            min_bytes: 0,
            largest: false,
            previews: true,
        }
    }
}
impl Filters {
    fn matches(&self, file: &Attachment, kinds: &BTreeSet<String>) -> bool {
        (if self.kind == "selected" {
            kinds.contains(&file.kind)
        } else {
            self.kind.is_empty() || self.kind == file.kind
        }) && (match self.status {
            0 => file.source_exists || file.copy.is_some(),
            1 => file.source_exists && !file.current,
            2 => file.copy.is_some(),
            3 => !file.source_exists && file.copy.is_none(),
            _ => true,
        }) && (self.min_bytes == 0 || file.bytes.is_some_and(|b| b >= self.min_bytes))
            && self.query.to_lowercase().split_whitespace().all(|word| {
                format!("{} {}", file.name, file.source)
                    .to_lowercase()
                    .contains(word)
            })
    }
    fn rows(&self, files: &[Attachment], kinds: &BTreeSet<String>) -> Vec<usize> {
        let mut rows: Vec<_> = (0..files.len())
            .filter(|&i| self.matches(&files[i], kinds))
            .collect();
        rows.sort_by(|&a, &b| {
            let x = &files[a];
            let y = &files[b];
            let order = if self.largest {
                y.bytes.cmp(&x.bytes)
            } else {
                x.name.to_lowercase().cmp(&y.name.to_lowercase())
            };
            order.then(x.source.cmp(&y.source))
        });
        rows
    }
}

pub fn size(bytes: Option<u64>) -> String {
    let Some(bytes) = bytes else {
        return "大小未知".into();
    };
    let mut value = bytes as f64;
    let units = ["B", "KB", "MB", "GB", "TB"];
    let mut index = 0;
    while value >= 1024.0 && index + 1 < units.len() {
        value /= 1024.0;
        index += 1;
    }
    if index == 0 {
        format!("{bytes} B")
    } else {
        format!("{value:.1} {}", units[index])
    }
}

pub enum Action {
    Reveal(PathBuf),
    Preview(PreviewImage),
}

pub fn show(
    ui: &mut egui::Ui,
    files: &[Attachment],
    filters: &mut Filters,
    kinds: &BTreeSet<String>,
    width: &mut u32,
) -> Option<Action> {
    ui.add(
        egui::TextEdit::singleline(&mut filters.query)
            .desired_width(f32::INFINITY)
            .hint_text("搜索附件名称或路径…"),
    );
    ui.horizontal_wrapped(|ui| {
        egui::ComboBox::from_id_salt("attachment-kind")
            .selected_text(if filters.kind == "selected" {
                "按复制类型"
            } else if filters.kind.is_empty() {
                "全部类型"
            } else {
                KINDS
                    .iter()
                    .find(|(k, _)| *k == filters.kind)
                    .map(|(_, name)| *name)
                    .unwrap_or("其他附件")
            })
            .show_ui(ui, |ui| {
                ui.selectable_value(&mut filters.kind, "selected".into(), "按所选复制类型");
                ui.selectable_value(&mut filters.kind, String::new(), "全部类型");
                for (key, label) in KINDS {
                    ui.selectable_value(&mut filters.kind, key.into(), label);
                }
            });
        let states = [
            "可用文件",
            "待复制 / 更新",
            "已有副本",
            "未找到",
            "全部状态",
        ];
        egui::ComboBox::from_id_salt("attachment-state")
            .selected_text(states[filters.status as usize])
            .show_ui(ui, |ui| {
                for (i, name) in states.iter().enumerate() {
                    ui.selectable_value(&mut filters.status, i as u8, *name);
                }
            });
        egui::ComboBox::from_id_salt("attachment-size")
            .selected_text(if filters.min_bytes == 0 {
                "大小不限".into()
            } else {
                format!("≥ {}", size(Some(filters.min_bytes)))
            })
            .show_ui(ui, |ui| {
                for (bytes, name) in [
                    (0, "大小不限"),
                    (10 * 1024 * 1024, "≥ 10 MB"),
                    (100 * 1024 * 1024, "≥ 100 MB"),
                    (1024 * 1024 * 1024, "≥ 1 GB"),
                ] {
                    ui.selectable_value(&mut filters.min_bytes, bytes, name);
                }
            });
        ui.checkbox(&mut filters.largest, "大文件优先");
        if ui.button("重置筛选").clicked() {
            *filters = Filters::default();
        }
    });
    ui.horizontal_wrapped(|ui| {
        ui.checkbox(&mut filters.previews, "缩略预览");
        ui.add_enabled(
            filters.previews,
            egui::Slider::new(width, 64..=240)
                .step_by(16.0)
                .text("预览大小"),
        );
    });
    let rows = filters.rows(files, kinds);
    let total: u64 = rows
        .iter()
        .filter_map(|&i| files[i].bytes)
        .fold(0u64, u64::saturating_add);
    let pending: u64 = rows
        .iter()
        .filter(|&&i| files[i].source_exists && !files[i].current)
        .filter_map(|&i| files[i].bytes)
        .fold(0u64, u64::saturating_add);
    let unknown = rows.iter().filter(|&&i| files[i].bytes.is_none()).count();
    ui.label(
        RichText::new(format!(
            "匹配 {} / {} 项 · 已知大小 {} · 尚未备份 {}{}",
            rows.len(),
            files.len(),
            size(Some(total)),
            size(Some(pending)),
            if unknown > 0 {
                format!(" · {unknown} 项大小未知")
            } else {
                String::new()
            }
        ))
        .strong(),
    );
    ui.label(RichText::new("大小取自最近的附件清单；精确新增空间以估算为准。列表筛选仅影响预览，复制按所选类型与会话范围执行。")
        .small().color(MUTED));
    if rows.is_empty() {
        ui.label("没有匹配的附件，可调整类型、状态或大小条件。");
        return None;
    }
    let mut action = None;
    let row_height = if filters.previews {
        (*width as f32 * 0.75 + 18.0).max(76.0)
    } else {
        76.0
    };
    egui::ScrollArea::vertical()
        .id_salt("attachment-list")
        .max_height(340.0)
        .min_scrolled_height(100.0)
        .show_rows(ui, row_height, rows.len(), |ui, range| {
            for index in range {
                let file = &files[rows[index]];
                ui.push_id(&file.source, |ui| {
                    ui.set_min_height(row_height - ui.spacing().item_spacing.y);
                    ui.horizontal_top(|ui| {
                        if filters.previews {
                            let bounds = egui::vec2(*width as f32, *width as f32 * 0.75);
                            let uri = file
                                .copy
                                .as_ref()
                                .filter(|path| {
                                    file.kind == "images"
                                        && path
                                            .extension()
                                            .and_then(|ext| ext.to_str())
                                            .is_some_and(|ext| {
                                                ["png", "jpg", "jpeg", "gif", "webp"]
                                                    .contains(&ext.to_ascii_lowercase().as_str())
                                            })
                                })
                                .and_then(|p| url::Url::from_file_path(p).ok())
                                .map(|u| format!("vault-{u}"));
                            ui.allocate_ui(bounds, |ui| {
                                if let Some(uri) = uri {
                                    let response = ui.add(
                                        egui::Image::new(&uri)
                                            .max_size(bounds)
                                            .fit_to_original_size(1.0)
                                            .sense(egui::Sense::click())
                                            .alt_text(&file.name),
                                    );
                                    if response.clicked() {
                                        action = Some(Action::Preview(PreviewImage {
                                            uri,
                                            alt: file.name.clone(),
                                        }));
                                    }
                                } else {
                                    egui::Frame::new().fill(PAPER).corner_radius(8).show(
                                        ui,
                                        |ui| {
                                            ui.set_min_size(bounds - egui::vec2(16.0, 16.0));
                                            ui.centered_and_justified(|ui| {
                                                ui.label(
                                                    RichText::new(
                                                        Path::new(&file.name)
                                                            .extension()
                                                            .and_then(|x| x.to_str())
                                                            .unwrap_or("FILE")
                                                            .to_uppercase(),
                                                    )
                                                    .strong()
                                                    .color(MUTED),
                                                );
                                            });
                                        },
                                    );
                                }
                            });
                        }
                        ui.vertical(|ui| {
                            ui.set_width(ui.available_width().max(80.0));
                            ui.allocate_ui_with_layout(
                                egui::vec2(ui.available_width(), 24.0),
                                egui::Layout::left_to_right(egui::Align::Center),
                                |ui| {
                                    let name_width = (ui.available_width() - 96.0).max(40.0);
                                    ui.allocate_ui_with_layout(
                                        egui::vec2(name_width, 24.0),
                                        egui::Layout::left_to_right(egui::Align::Center),
                                        |ui| {
                                            ui.add(
                                                egui::Label::new(
                                                    RichText::new(&file.name).strong().color(INK),
                                                )
                                                .truncate(),
                                            )
                                            .on_hover_text(&file.name);
                                        },
                                    );
                                    ui.label(RichText::new(size(file.bytes)).color(MUTED));
                                },
                            );
                            ui.horizontal(|ui| {
                                let label = KINDS
                                    .iter()
                                    .find(|(key, _)| *key == file.kind)
                                    .map(|(_, label)| *label)
                                    .unwrap_or("其他附件");
                                ui.label(
                                    RichText::new(format!("{label} · {}", file.status()))
                                        .small()
                                        .color(MUTED),
                                );
                                if let Some(path) = &file.copy {
                                    if ui.small_button("定位副本").clicked() {
                                        action = Some(Action::Reveal(path.clone()));
                                    }
                                }
                            });
                            ui.add(
                                egui::Label::new(RichText::new(&file.source).small().color(MUTED))
                                    .truncate(),
                            )
                            .on_hover_text(&file.source);
                        });
                    });
                    ui.add(egui::Separator::default().spacing(1.0));
                });
            }
        });
    action
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn filters_combine_kind_status_size_query_and_sort_without_changing_files() {
        let files = vec![
            Attachment {
                name: "大场景.blend".into(),
                source: "/fixture/art/大场景.blend".into(),
                kind: "models".into(),
                bytes: Some(200 * 1024 * 1024),
                copy: None,
                source_exists: true,
                current: false,
            },
            Attachment {
                name: "notes.md".into(),
                source: "/fixture/notes.md".into(),
                kind: "documents".into(),
                bytes: Some(0),
                copy: None,
                source_exists: false,
                current: false,
            },
        ];
        let mut filters = Filters::default();
        let kinds = BTreeSet::from(["models".into()]);
        assert_eq!(filters.rows(&files, &kinds), vec![0]);
        filters.query = "场景 art".into();
        filters.min_bytes = 100 * 1024 * 1024;
        filters.status = 1;
        assert_eq!(filters.rows(&files, &kinds), vec![0]);
        filters.min_bytes = 1024 * 1024 * 1024;
        assert!(filters.rows(&files, &kinds).is_empty());
        filters = Filters::default();
        filters.kind = "".into();
        filters.status = 3;
        assert_eq!(filters.rows(&files, &kinds), vec![1]);
        assert_eq!(size(None), "大小未知");
        assert_eq!(size(Some(0)), "0 B");
        assert_eq!(size(Some(2 * 1024 * 1024)), "2.0 MB");
        assert_eq!(
            entries(
                Path::new("/fixture"),
                &json!({"files": [{"source":"x","copy":"../../outside.png"}]}),
                None
            )[0]
            .copy,
            None
        );
    }
    #[test]
    fn embedded_images_are_listed_once_and_unsafe_copies_are_rejected() {
        let root = std::env::temp_dir().join(format!("vault-attachments-{}", std::process::id()));
        fs::create_dir_all(root.join("images")).unwrap();
        let name = "a".repeat(64) + ".png";
        let relative = format!("images/{name}");
        fs::write(root.join(&relative), b"fixture image").unwrap();
        let manifest = json!({"metadata": {"assets": [name]}, "files": [
            {"name": "photo.png", "source": "/previous/photo.png", "kind": "images", "exists": false, "copy": relative},
            {"source": "/outside", "copy": "../outside.png"}
        ]});
        let files = entries(&root, &manifest, None);
        assert_eq!(
            files.len(),
            2,
            "embedded assets and file references share one preview"
        );
        assert_eq!(files[0].bytes, Some(13));
        assert!(files[0].copy.is_some());
        assert!(files[1].copy.is_none());
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(root.join(&relative), root.join("images/link.png")).unwrap();
            assert!(archive_copy(&root, "images/link.png", None).is_none());
        }
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn previews_decode_without_rendering_a_conversation_and_support_configured_images() {
        let root =
            std::env::temp_dir().join(format!("vault-preview-loader-{}", std::process::id()));
        let external = root.join("自选图片库");
        fs::create_dir_all(&external).unwrap();
        let name = "b".repeat(64) + ".png";
        fs::write(
            external.join(&name),
            include_bytes!("../../assets/starvault-256.png"),
        )
        .unwrap();
        let manifest = json!({"metadata":{"assets":[name]}});
        let files = entries(&root, &manifest, Some(&external));
        let image = files[0]
            .copy
            .as_ref()
            .expect("explicitly configured image root");
        let uri = format!("vault-{}", url::Url::from_file_path(image).unwrap());
        let ctx = egui::Context::default();
        crate::setup(&ctx, 15.0);
        let mut loaded = false;
        for _ in 0..100 {
            match ctx
                .try_load_texture(&uri, egui::TextureOptions::LINEAR, Default::default())
                .expect("image loader installed independently of Markdown")
            {
                egui::load::TexturePoll::Ready { .. } => {
                    loaded = true;
                    break;
                }
                _ => std::thread::sleep(std::time::Duration::from_millis(2)),
            }
        }
        assert!(loaded);
        fs::remove_dir_all(root).unwrap();
    }
}
