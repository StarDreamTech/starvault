use super::{filter_combo, tool_name, Catalog, Filters, ACCENT, MUTED, SELECTED};
use eframe::egui::{self, RichText};
use std::collections::BTreeSet;

pub(super) fn show(ctx: &egui::Context, filters: &mut Filters, catalog: &Catalog) -> bool {
    let mut changed = false;
    let active = [
        &filters.tool,
        &filters.device,
        &filters.project,
        &filters.model,
        &filters.source,
        &filters.label,
    ]
    .iter()
    .filter(|value| !value.is_empty())
    .count()
        + usize::from(filters.days > 0);
    egui::SidePanel::left("filters")
        .default_width(218.0)
        .width_range(190.0..=260.0)
        .frame(
            egui::Frame::new()
                .fill(egui::Color32::WHITE)
                .inner_margin(14),
        )
        .show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("筛选").size(17.0).strong());
                if active > 0 {
                    ui.label(RichText::new(format!("{active} 项")).small().color(MUTED));
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui
                        .add_enabled(active > 0, egui::Button::new("清除").small())
                        .on_hover_text("清除筛选条件，保留搜索文字和归档标签页")
                        .clicked()
                    {
                        *filters = Filters {
                            query: filters.query.clone(),
                            archived: filters.archived,
                            ..Default::default()
                        };
                        changed = true;
                    }
                });
            });
            ui.add_space(8.0);
            ui.separator();
            egui::ScrollArea::vertical()
                .id_salt("filter-sidebar-scroll")
                .auto_shrink([false, false])
                .show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 6.0;
                    ui.label(RichText::new("对话来源").small().color(MUTED));
                    let tools: BTreeSet<_> = catalog
                        .sessions
                        .iter()
                        .map(|s| s.meta.tool.clone())
                        .filter(|tool| !tool.is_empty())
                        .collect();
                    ui.horizontal_wrapped(|ui| {
                        changed |= ui
                            .selectable_value(&mut filters.tool, String::new(), "全部来源")
                            .changed();
                        for tool in tools {
                            let name = tool_name(&tool);
                            changed |= ui.selectable_value(&mut filters.tool, tool, name).changed();
                        }
                    });
                    ui.add_space(6.0);
                    changed |= filter_combo(
                        ui,
                        "电脑",
                        &mut filters.device,
                        catalog.sessions.iter().map(|s| s.device.clone()).collect(),
                    );
                    changed |= filter_combo(
                        ui,
                        "项目",
                        &mut filters.project,
                        catalog
                            .sessions
                            .iter()
                            .map(|s| s.meta.project.clone())
                            .collect(),
                    );
                    changed |= filter_combo(
                        ui,
                        "模型",
                        &mut filters.model,
                        catalog
                            .sessions
                            .iter()
                            .flat_map(|s| s.meta.models.clone())
                            .collect(),
                    );
                    changed |= filter_combo(
                        ui,
                        "会话类型",
                        &mut filters.source,
                        catalog
                            .sessions
                            .iter()
                            .map(|s| s.meta.source.clone())
                            .collect(),
                    );
                    changed |= filter_combo(
                        ui,
                        "原始标签",
                        &mut filters.label,
                        catalog
                            .sessions
                            .iter()
                            .flat_map(|s| s.meta.labels.clone())
                            .collect(),
                    );
                    ui.add_space(6.0);
                    ui.label(RichText::new("更新时间").small().color(MUTED));
                    ui.horizontal_wrapped(|ui| {
                        for (days, label) in
                            [(0, "全部时间"), (1, "24 小时"), (7, "7 天"), (30, "30 天")]
                        {
                            changed |= ui
                                .selectable_value(&mut filters.days, days, label)
                                .changed();
                        }
                    });
                    let selected = [
                        ("tool", "来源", tool_name(&filters.tool)),
                        ("device", "电脑", filters.device.clone()),
                        ("project", "项目", filters.project.clone()),
                        ("model", "模型", filters.model.clone()),
                        ("source", "类型", filters.source.clone()),
                        ("label", "标签", filters.label.clone()),
                        (
                            "days",
                            "时间",
                            if filters.days > 0 {
                                format!("最近 {} 天", filters.days)
                            } else {
                                String::new()
                            },
                        ),
                    ];
                    if selected.iter().any(|(_, _, value)| !value.is_empty()) {
                        ui.add_space(8.0);
                        ui.separator();
                        ui.label(RichText::new("已筛选").small().color(MUTED));
                        let mut remove = None;
                        ui.horizontal_wrapped(|ui| {
                            for (field, label, value) in selected {
                                if value.is_empty() {
                                    continue;
                                }
                                if ui
                                    .add(
                                        egui::Button::new(
                                            RichText::new(format!("{label}：{value}  ×"))
                                                .small()
                                                .color(ACCENT),
                                        )
                                        .fill(SELECTED)
                                        .corner_radius(8)
                                        .truncate(),
                                    )
                                    .on_hover_text(format!("{label}：{value}\n点击移除此条件"))
                                    .clicked()
                                {
                                    remove = Some(field);
                                }
                            }
                        });
                        if let Some(field) = remove {
                            match field {
                                "tool" => filters.tool.clear(),
                                "device" => filters.device.clear(),
                                "project" => filters.project.clear(),
                                "model" => filters.model.clear(),
                                "source" => filters.source.clear(),
                                "label" => filters.label.clear(),
                                "days" => filters.days = 0,
                                _ => unreachable!(),
                            }
                            changed = true;
                        }
                    }
                });
        });
    changed
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Session, SessionMeta};

    fn labels(output: &egui::FullOutput) -> Vec<(String, egui::Rect)> {
        fn visit(shape: &egui::Shape, result: &mut Vec<(String, egui::Rect)>) {
            match shape {
                egui::Shape::Text(text) => {
                    result.push((text.galley.text().into(), shape.visual_bounding_rect()));
                }
                egui::Shape::Vec(items) => {
                    for item in items {
                        visit(item, result);
                    }
                }
                _ => {}
            }
        }
        let mut result = Vec::new();
        for shape in &output.shapes {
            visit(&shape.shape, &mut result);
        }
        result
    }

    #[test]
    fn sidebar_filters_are_visible_and_clickable_without_opening_a_menu() {
        let catalog = Catalog {
            sessions: ["codex", "claude"]
                .into_iter()
                .map(|tool| Session {
                    meta: SessionMeta {
                        tool: tool.into(),
                        archived: Some(true),
                        ..Default::default()
                    },
                    display_title: String::new(),
                    branch: format!("demo-{tool}"),
                    device: "demo".into(),
                    path: Default::default(),
                    markdown: String::new(),
                    search: "needle".into(),
                })
                .collect(),
            ..Default::default()
        };
        for width in [1320.0, 980.0] {
            let ctx = egui::Context::default();
            crate::setup(&ctx, 15.0);
            let mut filters = Filters {
                query: "needle".into(),
                archived: 2,
                ..Default::default()
            };
            let mut source_position = egui::Pos2::ZERO;
            let mut clear_position = egui::Pos2::ZERO;
            for frame in 0..8 {
                let events = match frame {
                    2 | 3 | 5 | 6 => {
                        let pos = if frame <= 3 {
                            source_position
                        } else {
                            clear_position
                        };
                        vec![
                            egui::Event::PointerMoved(pos),
                            egui::Event::PointerButton {
                                pos,
                                button: egui::PointerButton::Primary,
                                pressed: frame == 2 || frame == 5,
                                modifiers: egui::Modifiers::NONE,
                            },
                        ]
                    }
                    _ => vec![],
                };
                let mut changed = false;
                let mut content_width = 0.0;
                let output = ctx.run(
                    egui::RawInput {
                        screen_rect: Some(egui::Rect::from_min_size(
                            egui::Pos2::ZERO,
                            egui::vec2(width, 640.0),
                        )),
                        time: Some(frame as f64 / 60.0),
                        events,
                        ..Default::default()
                    },
                    |ctx| {
                        changed = show(ctx, &mut filters, &catalog);
                        egui::SidePanel::left("test-list")
                            .default_width(280.0)
                            .width_range(
                                220.0..=(ctx.available_rect().width() - 500.0).clamp(220.0, 380.0),
                            )
                            .show(ctx, |ui| {
                                ui.label("会话列表");
                            });
                        egui::CentralPanel::default().show(ctx, |ui| {
                            content_width = ui.available_width();
                            ui.label("阅读内容");
                        });
                    },
                );
                let labels = labels(&output);
                let position =
                    |text: &str| labels.iter().find(|(value, _)| value == text).unwrap().1;
                source_position = position("Codex").center();
                clear_position = position("清除").center();
                for label in [
                    "筛选",
                    "对话来源",
                    "电脑",
                    "项目",
                    "模型",
                    "会话类型",
                    "原始标签",
                    "更新时间",
                ] {
                    assert!(
                        position(label).right() < position("会话列表").left(),
                        "{label} stays in its own sidebar"
                    );
                    assert!(
                        position(label).bottom() < 640.0,
                        "{label} is visible without opening a menu"
                    );
                }
                assert!(
                    content_width >= 460.0,
                    "reading space at width {width}: {content_width}"
                );
                assert_eq!(filters.query, "needle");
                assert_eq!(filters.archived, 2);
                if frame == 3 {
                    assert!(changed);
                    assert_eq!(filters.tool, "codex");
                    assert_eq!(
                        catalog
                            .sessions
                            .iter()
                            .filter(|s| filters.matches(s))
                            .count(),
                        1
                    );
                }
                if frame == 6 {
                    assert!(changed);
                    assert!(filters.tool.is_empty());
                    assert_eq!(
                        catalog
                            .sessions
                            .iter()
                            .filter(|s| filters.matches(s))
                            .count(),
                        2
                    );
                }
            }
        }
    }
}
