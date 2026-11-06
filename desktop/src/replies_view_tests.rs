use super::*;

fn render(
    ctx: &egui::Context,
    cards: &mut [MessageCard],
    cache: &mut CommonMarkCache,
    target: &mut Option<usize>,
    events: Vec<egui::Event>,
    frame: usize,
) -> egui::FullOutput {
    ctx.run(
        egui::RawInput {
            screen_rect: Some(egui::Rect::from_min_size(
                egui::Pos2::ZERO,
                egui::vec2(700.0, 500.0),
            )),
            time: Some(frame as f64 / 60.0),
            events,
            ..Default::default()
        },
        |ctx| {
            egui::CentralPanel::default().show(ctx, |ui| {
                show_message_cards(ui, cards, cache, 320, *target, target);
            });
        },
    )
}

fn text_shapes(output: &egui::FullOutput) -> Vec<(String, egui::Rect)> {
    fn visit(shape: &egui::Shape, result: &mut Vec<(String, egui::Rect)>) {
        match shape {
            egui::Shape::Text(text) => {
                result.push((text.galley.text().to_owned(), shape.visual_bounding_rect()));
            }
            egui::Shape::Vec(shapes) => {
                for shape in shapes {
                    visit(shape, result);
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
fn reply_groups_preserve_original_indices_and_stop_at_real_requests() {
    let markdown = concat!(
        "## 用户 · \n请完成第一轮。\n",
        "## 助手 · \n过程说明。\n```markdown\n## 用户 · 示例\n```\n",
        "![过程截图](vault-file:///fixture.png)\n",
        "## 用户 · \n<environment_context><cwd>/fixture</cwd></environment_context>\n",
        "## 助手 · \n已完成第一轮。\n",
        "## 用户 · \n<environment_context><cwd>/fixture</cwd></environment_context>\n真实的新问题。\n",
        "## 助手 · \n单条回复保持展示。\n",
        "## 用户 · \n![新问题截图](vault-file:///request.png)\n",
        "## 助手 · \n第二轮过程。\n",
        "## 助手 · \n已完成第二轮。\n",
    );
    let cards = message_cards(markdown);
    assert_eq!(cards.len(), 9, "role examples inside code are not messages");
    assert!(cards[2].context.is_some());
    assert!(
        cards[4].context.is_none(),
        "mixed context is a real request"
    );
    assert!(
        cards[6].context.is_none(),
        "an image-only request starts a turn"
    );
    let before: Vec<_> = cards.iter().map(|card| card.markdown.clone()).collect();
    assert_eq!(
        assistant_reply_groups(&cards),
        BTreeMap::from([(1, (3, 1)), (7, (8, 1))])
    );
    assert_eq!(
        before,
        cards
            .iter()
            .map(|card| card.markdown.clone())
            .collect::<Vec<_>>()
    );
    assert!(cards[1].markdown.contains("## 用户 · 示例"));
    let (_, images) = thumbnail_markdown(&cards[1].markdown);
    assert_eq!(
        images.len(),
        1,
        "hidden process images remain available on expansion"
    );
    let nodes = timeline::extract(markdown);
    assert!(nodes.iter().any(|node| node.message_index == 3));
    assert!(nodes.iter().any(|node| node.message_index == 8));
}

#[test]
fn reply_group_button_reveals_and_hides_process_text() {
    let ctx = egui::Context::default();
    let mut cards = message_cards(concat!(
        "## 用户 · \nQuestion.\n",
        "## 助手 · \nEARLY_PROCESS_MARKER\n",
        "## 助手 · \nMIDDLE_PROCESS_MARKER\n",
        "## 助手 · \nLATEST_REPLY_MARKER\n",
    ));
    let mut cache = CommonMarkCache::default();
    let mut target = None;
    let mut output = render(&ctx, &mut cards, &mut cache, &mut target, vec![], 0);
    for frame in 1..3 {
        output = render(&ctx, &mut cards, &mut cache, &mut target, vec![], frame);
    }
    for (expand, frame) in [(true, 3), (false, 6)] {
        let text = text_shapes(&output);
        assert!(text
            .iter()
            .any(|(text, _)| text.contains("LATEST_REPLY_MARKER")));
        assert_eq!(
            text.iter()
                .any(|(text, _)| text.contains("EARLY_PROCESS_MARKER")),
            !expand
        );
        let label = if expand {
            "展开过程回复"
        } else {
            "收起过程回复"
        };
        let pos = text
            .iter()
            .find(|(text, _)| text.contains(label))
            .expect("visible disclosure button")
            .1
            .center();
        render(
            &ctx,
            &mut cards,
            &mut cache,
            &mut target,
            vec![
                egui::Event::PointerMoved(pos),
                egui::Event::PointerButton {
                    pos,
                    button: egui::PointerButton::Primary,
                    pressed: true,
                    modifiers: Default::default(),
                },
            ],
            frame,
        );
        render(
            &ctx,
            &mut cards,
            &mut cache,
            &mut target,
            vec![egui::Event::PointerButton {
                pos,
                button: egui::PointerButton::Primary,
                pressed: false,
                modifiers: Default::default(),
            }],
            frame + 1,
        );
        output = render(&ctx, &mut cards, &mut cache, &mut target, vec![], frame + 2);
        assert_eq!(cards[1].replies_expanded, expand);
        let text = text_shapes(&output);
        assert_eq!(
            text.iter()
                .any(|(text, _)| text.contains("EARLY_PROCESS_MARKER")),
            expand
        );
        assert_eq!(
            text.iter()
                .any(|(text, _)| text.contains("MIDDLE_PROCESS_MARKER")),
            expand
        );
        assert!(text
            .iter()
            .any(|(text, _)| text.contains("LATEST_REPLY_MARKER")));
    }
}

#[test]
fn jumping_to_a_hidden_process_opens_its_group_and_keeps_original_index() {
    let markdown = (0..40)
        .map(|turn| {
            format!(
        "## 用户 · \nQuestion {turn}.\n## 助手 · \nProcess {turn}.\n## 助手 · \nLatest {turn}.\n"
    )
        })
        .collect::<String>();
    let ctx = egui::Context::default();
    let mut cards = message_cards(&markdown);
    let mut cache = CommonMarkCache::default();
    let target_index = 37 * 3 + 1;
    let mut target = Some(target_index);
    assert_eq!(cards[target_index].markdown.trim(), "Process 37.");
    for frame in 0..120 {
        render(&ctx, &mut cards, &mut cache, &mut target, vec![], frame);
        assert!(cards[target_index].replies_expanded);
        assert!(
            cards[target_index].size.x > 0.0,
            "target bypasses offscreen virtualization"
        );
        if target.is_none() {
            break;
        }
    }
    assert!(
        target.is_none(),
        "jump settles when the original process is visible"
    );
    assert!(!cards[1].replies_expanded, "unrelated turns remain folded");
    assert_eq!(cards[target_index].markdown.trim(), "Process 37.");
}
