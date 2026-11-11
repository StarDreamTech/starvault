//! Fixed product material: no archive, configuration or conversation inputs.
use super::{brand, egui, Color32, PreviewImage, RichText, ACCENT, LINE, MUTED, NAVY};
use serde::Deserialize;
use std::{
    borrow::Cow,
    fs,
    io::Write,
    path::Path,
    sync::atomic::{AtomicU64, Ordering},
};

pub const PROJECT: &str = "https://github.com/StarDreamTech/starvault";
const CATALOG: &str = include_str!("../../assets/share/catalog.json");
const LANGUAGES: [&str; 2] = ["zh-CN", "en-US"];
const THEMES: [&str; 3] = ["overview", "ownership", "context"];

#[derive(Deserialize)]
struct Catalog {
    texts: Vec<ShareText>,
    images: Vec<ShareImage>,
}
#[derive(Deserialize)]
struct ShareText {
    locale: String,
    kind: String,
    title: String,
    body: String,
}
#[derive(Deserialize)]
struct ShareImage {
    id: String,
    locale: String,
    title: String,
    description: String,
    filename: String,
    width: u32,
    height: u32,
    bytes: usize,
}

fn image_bytes(id: &str) -> Result<&'static [u8], String> {
    match id {
        "overview" => Ok(include_bytes!("../../assets/share/overview.png")),
        "ownership" => Ok(include_bytes!("../../assets/share/ownership.png")),
        "context" => Ok(include_bytes!("../../assets/share/context.png")),
        "overview-en" => Ok(include_bytes!("../../assets/share/overview-en.png")),
        "ownership-en" => Ok(include_bytes!("../../assets/share/ownership-en.png")),
        "context-en" => Ok(include_bytes!("../../assets/share/context-en.png")),
        _ => Err("未找到这份分享图片".into()),
    }
}
fn decode_image(item: &ShareImage) -> Result<image::RgbaImage, String> {
    let bytes = image_bytes(&item.id)?;
    if bytes.len() != item.bytes
        || bytes.len() > 3 * 1024 * 1024
        || u64::from(item.width) * u64::from(item.height) > 2_000_000
    {
        return Err("分享图片信息不符，请重新安装软件".into());
    }
    let image = image::load_from_memory_with_format(bytes, image::ImageFormat::Png)
        .map_err(|e| format!("无法读取分享图片：{e}"))?
        .into_rgba8();
    if image.dimensions() != (item.width, item.height) {
        return Err("分享图片尺寸不符".into());
    }
    Ok(image)
}
fn write_image(path: &Path, bytes: &[u8]) -> Result<(), String> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let parent = path.parent().ok_or("请选择保存文件夹")?;
    let temporary = parent.join(format!(
        ".starvault-share-{}-{}.tmp",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|e| format!("无法准备保存：{e}"))?;
    let result = (|| {
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.map_err(|e| format!("图片未保存：{e}"))
}

pub struct Panel {
    catalog: Catalog,
    language: usize,
    text_kind: usize,
    image_kind: usize,
    status: String,
    error: bool,
    // Linux needs the clipboard owner alive to serve later paste requests.
    clipboard: Option<arboard::Clipboard>,
}
impl Default for Panel {
    fn default() -> Self {
        Self {
            catalog: serde_json::from_str(CATALOG).expect("bundled share catalog"),
            language: 0,
            text_kind: 0,
            image_kind: 0,
            status: String::new(),
            error: false,
            clipboard: None,
        }
    }
}
impl Panel {
    fn selected_text(&self) -> &ShareText {
        self.catalog
            .texts
            .iter()
            .find(|text| {
                text.locale == LANGUAGES[self.language]
                    && text.kind
                        == if self.text_kind == 0 {
                            "short"
                        } else {
                            "detailed"
                        }
            })
            .expect("paired share copy")
    }
    fn selected_image(&self) -> &ShareImage {
        let id = format!(
            "{}{}",
            THEMES[self.image_kind],
            if self.language == 0 { "" } else { "-en" }
        );
        self.catalog
            .images
            .iter()
            .find(|image| image.id == id && image.locale == LANGUAGES[self.language])
            .expect("paired share image")
    }
    fn clipboard(&mut self) -> Result<&mut arboard::Clipboard, String> {
        if self.clipboard.is_none() {
            self.clipboard =
                Some(arboard::Clipboard::new().map_err(|e| format!("剪贴板暂不可用：{e}"))?);
        }
        Ok(self.clipboard.as_mut().unwrap())
    }
    fn feedback(&mut self, result: Result<String, String>) {
        match result {
            Ok(message) => {
                self.error = false;
                self.status = message;
            }
            Err(error) => {
                self.error = true;
                self.status = error;
            }
        }
    }
    pub fn show(&mut self, ui: &mut egui::Ui) -> Option<PreviewImage> {
        let mut preview = None;
        egui::Frame::new()
            .fill(NAVY)
            .corner_radius(12)
            .inner_margin(16)
            .show(ui, |ui| {
                ui.set_min_width(ui.available_width());
                ui.horizontal(|ui| {
                    brand::mark(ui, 40.0);
                    ui.vertical(|ui| {
                        ui.label(
                            RichText::new("分享星匣 StarVault")
                                .strong()
                                .size(20.0)
                                .color(Color32::WHITE),
                        );
                        ui.label(
                            RichText::new("你的 AI 记忆，始终属于你。")
                                .color(Color32::from_rgb(204, 215, 237)),
                        );
                    });
                });
            });
        ui.horizontal_wrapped(|ui| {
            ui.label("素材语言");
            let previous = self.language;
            ui.selectable_value(&mut self.language, 0, "简体中文");
            ui.selectable_value(&mut self.language, 1, "English");
            if previous != self.language {
                self.status.clear();
            }
        });
        ui.separator();
        if !self.status.is_empty() {
            ui.add(
                egui::Label::new(RichText::new(&self.status).color(if self.error {
                    Color32::DARK_RED
                } else {
                    ACCENT
                }))
                .wrap(),
            );
        }
        ui.columns(2, |columns| {
            let left = &mut columns[0];
            left.heading("分享文案");
            left.horizontal_wrapped(|ui| {
                let short_title = self
                    .catalog
                    .texts
                    .iter()
                    .find(|text| text.locale == LANGUAGES[self.language] && text.kind == "short")
                    .unwrap()
                    .title
                    .clone();
                let detailed_title = self
                    .catalog
                    .texts
                    .iter()
                    .find(|text| text.locale == LANGUAGES[self.language] && text.kind == "detailed")
                    .unwrap()
                    .title
                    .clone();
                let short = ui
                    .selectable_value(&mut self.text_kind, 0, short_title)
                    .changed();
                let detailed = ui
                    .selectable_value(&mut self.text_kind, 1, detailed_title)
                    .changed();
                if short || detailed {
                    self.status.clear();
                }
            });
            let body = self.selected_text().body.clone();
            egui::ScrollArea::vertical()
                .id_salt("share-copy")
                .max_height(340.0)
                .show(left, |ui| {
                    ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                        ui.add(egui::Label::new(&body).wrap().selectable(true));
                    });
                });
            left.add_space(10.0);
            left.horizontal_wrapped(|ui| {
                if ui.button("复制文案").clicked() {
                    let result = self
                        .clipboard()
                        .and_then(|board| {
                            board
                                .set_text(body.clone())
                                .map_err(|e| format!("文案未复制：{e}"))
                        })
                        .map(|_| "文案已复制，可以粘贴给朋友。".into());
                    self.feedback(result);
                }
                if ui.button("复制项目链接").clicked() {
                    let result = self
                        .clipboard()
                        .and_then(|board| {
                            board
                                .set_text(PROJECT)
                                .map_err(|e| format!("链接未复制：{e}"))
                        })
                        .map(|_| "项目链接已复制。".into());
                    self.feedback(result);
                }
            });
            left.hyperlink_to("项目与下载 ↗", PROJECT);
            let right = &mut columns[1];
            right.heading("产品介绍图");
            right.horizontal_wrapped(|ui| {
                for (i, title) in ["产品总览", "换机迁移", "AI 历史资料"].iter().enumerate()
                {
                    if ui
                        .selectable_value(&mut self.image_kind, i, *title)
                        .changed()
                    {
                        self.status.clear();
                    }
                }
            });
            let item = self.selected_image();
            let id = item.id.clone();
            let filename = item.filename.clone();
            let bytes = image_bytes(&id).expect("fixed share image");
            let uri = format!("bytes://starvault-share/{id}.png");
            let response = right.add(
                egui::Image::from_bytes(uri.clone(), bytes)
                    .max_size(egui::vec2(right.available_width(), 340.0))
                    .fit_to_original_size(1.0)
                    .sense(egui::Sense::click())
                    .alt_text(&item.description),
            );
            if response.clicked() {
                preview = Some(PreviewImage {
                    uri,
                    alt: item.description.clone(),
                });
            }
            right.label(
                RichText::new(format!(
                    "{} · {} × {} · {}",
                    item.title,
                    item.width,
                    item.height,
                    super::attachments_view::size(Some(bytes.len() as u64))
                ))
                .small()
                .color(MUTED),
            );
            right.horizontal_wrapped(|ui| {
                if ui.button("复制图片").clicked() {
                    let result = decode_image(self.selected_image())
                        .and_then(|image| {
                            let (width, height) = image.dimensions();
                            self.clipboard()?
                                .set_image(arboard::ImageData {
                                    width: width as usize,
                                    height: height as usize,
                                    bytes: Cow::Owned(image.into_raw()),
                                })
                                .map_err(|e| format!("图片未复制，可改用保存图片：{e}"))
                        })
                        .map(|_| "图片已复制，可粘贴到支持图片的应用。".into());
                    self.feedback(result);
                }
                if ui.button("保存图片").clicked() {
                    if let Some(path) = rfd::FileDialog::new()
                        .add_filter("PNG 图片", &["png"])
                        .set_file_name(&filename)
                        .save_file()
                    {
                        self.feedback(
                            write_image(&path, bytes)
                                .map(|_| format!("图片已保存：{}", path.display())),
                        );
                    } else {
                        self.feedback(Ok("已取消保存，没有写入文件。".into()));
                    }
                }
            });
        });
        ui.add_space(8.0);
        ui.painter().hline(
            ui.available_rect_before_wrap().x_range(),
            ui.cursor().top(),
            egui::Stroke::new(1.0, LINE),
        );
        ui.label(
            RichText::new(
                "素材为固定原创样例，预览、复制和保存使用同一份图片。复制或保存后，由你选择分享。",
            )
            .small()
            .color(MUTED),
        );
        preview
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_language_has_copy_and_valid_fixed_posters() {
        let mut panel = Panel::default();
        assert_eq!(panel.catalog.texts.len(), 4);
        assert_eq!(panel.catalog.images.len(), 6);
        for language in 0..2 {
            panel.language = language;
            for kind in 0..2 {
                panel.text_kind = kind;
                let text = panel.selected_text();
                assert!(!text.title.is_empty());
                assert!(text.body.contains(PROJECT));
            }
            for kind in 0..3 {
                panel.image_kind = kind;
                let image = panel.selected_image();
                assert_eq!(image.locale, LANGUAGES[language]);
                assert_eq!(decode_image(image).unwrap().dimensions(), (1080, 1440));
            }
        }
        for id in ["../overview", "/etc/passwd", "unknown"] {
            assert!(image_bytes(id).is_err());
        }
    }
    #[test]
    fn saves_the_exact_png_and_failure_never_claims_success() {
        let panel = Panel::default();
        let root =
            std::env::temp_dir().join(format!("starvault-share-save-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("星匣.png");
        let png = image_bytes(&panel.selected_image().id).unwrap();
        write_image(&path, png).unwrap();
        assert_eq!(fs::read(&path).unwrap(), png);
        assert!(write_image(&root.join("missing/fail.png"), png).is_err());
        assert_eq!(fs::read_dir(&root).unwrap().count(), 1);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn switching_material_language_preserves_the_selected_theme_and_never_opens_clipboard() {
        fn texts(shape: &egui::Shape, result: &mut Vec<(String, egui::Rect)>) {
            match shape {
                egui::Shape::Text(text) => {
                    result.push((text.galley.text().into(), shape.visual_bounding_rect()))
                }
                egui::Shape::Vec(items) => {
                    for item in items {
                        texts(item, result);
                    }
                }
                _ => {}
            }
        }
        let ctx = egui::Context::default();
        crate::setup(&ctx, 15.0);
        let mut panel = Panel::default();
        panel.image_kind = 2;
        let mut position = None;
        for frame in 0..4 {
            let events = if frame >= 2 {
                let pos = position.unwrap();
                vec![
                    egui::Event::PointerMoved(pos),
                    egui::Event::PointerButton {
                        pos,
                        button: egui::PointerButton::Primary,
                        pressed: frame == 2,
                        modifiers: egui::Modifiers::NONE,
                    },
                ]
            } else {
                vec![]
            };
            let output = ctx.run(
                egui::RawInput {
                    screen_rect: Some(egui::Rect::from_min_size(
                        egui::Pos2::ZERO,
                        egui::vec2(840.0, 760.0),
                    )),
                    time: Some(frame as f64 / 60.0),
                    events,
                    ..Default::default()
                },
                |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| {
                        panel.show(ui);
                    });
                },
            );
            let mut labels = Vec::new();
            for shape in output.shapes {
                texts(&shape.shape, &mut labels);
            }
            position = labels
                .into_iter()
                .find(|(text, _)| text == "English")
                .map(|(_, rect)| rect.center());
        }
        assert_eq!(panel.selected_text().locale, "en-US");
        assert_eq!(panel.selected_image().id, "context-en");
        assert!(
            panel.clipboard.is_none(),
            "rendering or switching cannot access the system clipboard"
        );
    }
}
