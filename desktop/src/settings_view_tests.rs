use super::*;

#[test]
fn github_icon_supports_keyboard_activation() {
    let ctx = egui::Context::default();
    for frame in 0..2 {
        let output = ctx.run(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    egui::vec2(400.0, 100.0),
                )),
                events: if frame == 0 {
                    vec![]
                } else {
                    vec![egui::Event::Key {
                        key: egui::Key::Enter,
                        physical_key: None,
                        pressed: true,
                        repeat: false,
                        modifiers: egui::Modifiers::NONE,
                    }]
                },
                ..Default::default()
            },
            |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| {
                    let response = brand::github_link(ui);
                    if frame == 0 {
                        response.request_focus();
                    }
                });
            },
        );
        if frame == 1 {
            assert!(output
                .platform_output
                .commands
                .iter()
                .any(|command| matches!(command,
                    egui::OutputCommand::OpenUrl(url) if url.url == share::PROJECT
                )));
        }
    }
}

#[test]
fn github_icon_opens_only_the_public_source_repository() {
    let ctx = egui::Context::default();
    let mut position = egui::Pos2::ZERO;
    for frame in 0..3 {
        let output = ctx.run(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    egui::vec2(400.0, 100.0),
                )),
                events: if frame == 0 {
                    vec![]
                } else {
                    vec![
                        egui::Event::PointerMoved(position),
                        egui::Event::PointerButton {
                            pos: position,
                            button: egui::PointerButton::Primary,
                            pressed: frame == 1,
                            modifiers: egui::Modifiers::NONE,
                        },
                    ]
                },
                ..Default::default()
            },
            |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| {
                    let response = brand::github_link(ui);
                    position = response.rect.center();
                    assert!(response.rect.width() >= 32.0);
                });
            },
        );
        let urls: Vec<_> = output
            .platform_output
            .commands
            .iter()
            .filter_map(|command| {
                if let egui::OutputCommand::OpenUrl(url) = command {
                    Some(url)
                } else {
                    None
                }
            })
            .collect();
        if frame == 2 {
            assert_eq!(urls.len(), 1);
            assert_eq!(urls[0].url, "https://github.com/StarDreamTech/starvault");
            assert!(urls[0].new_tab);
        } else {
            assert!(urls.is_empty());
        }
    }
}

fn labels(output: &egui::FullOutput) -> Vec<(String, egui::Rect)> {
    fn visit(shape: &egui::Shape, result: &mut Vec<(String, egui::Rect)>) {
        match shape {
            egui::Shape::Text(text) => {
                result.push((text.galley.text().into(), shape.visual_bounding_rect()))
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
fn settings_tabs_stay_on_the_left_and_switch_the_right_panel_when_resized() {
    let ctx = egui::Context::default();
    setup(&ctx, 15.0);
    let mut selected = SettingsTab::Images;
    let mut click = None;
    let mut sidebar_position = None;
    for frame in 0..7 {
        let events = if frame == 2 || frame == 3 {
            let pos = click.unwrap();
            vec![
                egui::Event::PointerMoved(pos),
                egui::Event::PointerButton {
                    pos,
                    button: egui::PointerButton::Primary,
                    pressed: frame == 2,
                    modifiers: egui::Modifiers::NONE,
                },
            ]
        } else if frame == 5 {
            vec![
                egui::Event::PointerMoved(egui::pos2(300.0, 150.0)),
                egui::Event::MouseWheel {
                    unit: egui::MouseWheelUnit::Point,
                    delta: egui::vec2(0.0, -300.0),
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
                    egui::vec2(if frame >= 4 { 720.0 } else { 1020.0 }, 650.0),
                )),
                time: Some(frame as f64 / 60.0),
                events,
                ..Default::default()
            },
            |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| {
                    selected = settings_layout(ui, selected, |ui, tab| {
                        ui.label(if tab == SettingsTab::Images {
                            "图片内容"
                        } else {
                            "分享内容"
                        });
                        for i in 0..50 {
                            ui.label(format!("内容行 {i}"));
                        }
                    });
                });
            },
        );
        let labels = labels(&output);
        let image = labels
            .iter()
            .find(|(text, _)| text == "图片显示")
            .unwrap()
            .1;
        let share = labels
            .iter()
            .find(|(text, _)| text == "分享软件")
            .unwrap()
            .1;
        let content = labels
            .iter()
            .find(|(text, _)| text.ends_with("内容") || text.starts_with("内容行"))
            .unwrap()
            .1;
        assert!(
            (image.center().x - share.center().x).abs() < 1.0,
            "tabs share one left column: frame={frame}, image={image:?}, share={share:?}"
        );
        assert!(share.top() > image.bottom(), "navigation remains vertical");
        assert!(
            content.left() > share.right() + 20.0,
            "content stays to the right"
        );
        if frame == 4 {
            sidebar_position = Some(share.center());
        }
        if frame == 6 {
            assert_eq!(Some(share.center()), sidebar_position);
            assert!(
                !labels.iter().any(|(text, _)| text == "分享内容"),
                "only the right panel scrolls"
            );
        }
        click = Some(share.center());
    }
    assert!(selected == SettingsTab::Share);
}
