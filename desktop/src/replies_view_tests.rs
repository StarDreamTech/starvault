use super::*;

#[test]
fn original_metadata_folds_injections_and_preserves_human_requests() {
    let root =
        std::env::temp_dir().join(format!("starvault-message-metadata-{}", std::process::id()));
    fs::create_dir_all(&root).unwrap();
    let skill = "Base directory for this skill: /fixture/skills/browser\n\n# Built-in browser\n\nSKILL_INJECTION_MARKER\u{2028}second line";
    let relay = "<relay from=\"coordinator\"><note>COORDINATOR_MARKER</note></relay>";
    let wake = "<wake reason=\"mention\"><project id=\"demo\" type=\"project\"><message from=\"human\" trigger=\"true\">请写一个教程，保留 &lt;code&gt; 和 **重点**。</message></project></wake>";
    let markdown = format!("## 用户 · first\n{relay}\n## 用户 · second\n{skill}\n## 助手 · third\nVISIBLE_RESULT_MARKER\n## 用户 · fourth\n{wake}\n");
    let rows = [
        serde_json::json!({"isMeta":true,"timestamp":"first","message":{"role":"user","content":relay}}),
        serde_json::json!({"isMeta":true,"timestamp":"second","message":{"role":"user","content":[{"type":"text","text":skill}]}}),
        serde_json::json!({"timestamp":"third","message":{"role":"assistant","content":[{"type":"text","text":"VISIBLE_RESULT_MARKER"}]}}),
        serde_json::json!({"isMeta":true,"turnOrigin":"human","timestamp":"fourth","message":{"role":"user","content":wake}}),
        // A conflicting timestamp or incomplete sidecar must not classify by position.
        serde_json::json!({"isMeta":true,"timestamp":"fourth","message":{"role":"user","content":"different text"}}),
    ];
    fs::write(
        root.join("dialogue.jsonl"),
        rows.iter()
            .map(|row| format!("{row}\n"))
            .collect::<String>(),
    )
    .unwrap();
    let mut cards = message_cards(&markdown);
    message_metadata::apply(&mut cards, &root.join("manifest.json"));
    assert_eq!(cards.len(), 4);
    assert_eq!(cards[0].context, Some("Agent 协作"));
    assert_eq!(cards[1].context, Some("Skill 上下文"));
    assert_eq!(
        message_metadata::title(&cards[1]),
        "Skill · Built-in browser"
    );
    assert!(cards[2].context.is_none());
    assert!(cards[3].context.is_none());
    assert_eq!(cards[3].markdown, "请写一个教程，保留 <code> 和 **重点**。");
    assert_eq!(cards[3].original.trim(), wake);
    assert_eq!(cards[3].attachment_context.trim(), wake);
    assert_eq!(cards[1].original.trim(), skill);

    let ctx = egui::Context::default();
    let mut cache = CommonMarkCache::default();
    let mut target = None;
    let mut output = render(&ctx, &mut cards, &mut cache, &mut target, vec![], 0);
    for frame in 1..3 {
        output = render(&ctx, &mut cards, &mut cache, &mut target, vec![], frame);
    }
    let texts = text_shapes(&output);
    assert!(texts
        .iter()
        .any(|(text, _)| text.contains("VISIBLE_RESULT_MARKER")));
    assert!(!texts
        .iter()
        .any(|(text, _)| text.contains("SKILL_INJECTION_MARKER")
            || text.contains("COORDINATOR_MARKER")));
    assert!(
        cards[0].size.y < 60.0 && cards[1].size.y < 60.0,
        "injections take compact rows"
    );
    let position = texts
        .iter()
        .find(|(text, _)| text.contains("Skill · Built-in browser"))
        .unwrap()
        .1
        .center();
    for (frame, pressed) in [(3, true), (4, false)] {
        render(
            &ctx,
            &mut cards,
            &mut cache,
            &mut target,
            vec![
                egui::Event::PointerMoved(position),
                egui::Event::PointerButton {
                    pos: position,
                    button: egui::PointerButton::Primary,
                    pressed,
                    modifiers: Default::default(),
                },
            ],
            frame,
        );
    }
    for frame in 5..30 {
        output = render(&ctx, &mut cards, &mut cache, &mut target, vec![], frame);
    }
    assert!(
        text_shapes(&output)
            .iter()
            .any(|(text, _)| text.contains("SKILL_INJECTION_MARKER")),
        "expanded context keeps its original text"
    );

    let mut ambiguous = message_cards(&format!("## 用户 · second\n{skill}\n\n这是我的问题。\n"));
    message_metadata::apply(&mut ambiguous, &root.join("manifest.json"));
    assert!(
        ambiguous[0].context.is_none(),
        "mixed or unmatched text remains readable"
    );
    let image = format!("![图片](../../../images/{}.png)", "a".repeat(64));
    let suffix = format!("\n\n### 图片 · 2026-11-10T02:35:00Z\n\n{image}\n\n{image}\n");
    let mut with_screenshots = message_cards(&format!("## 用户 · second\n{skill}{suffix}"));
    message_metadata::apply(&mut with_screenshots, &root.join("manifest.json"));
    assert_eq!(with_screenshots[0].context, Some("Skill 上下文"));
    assert_eq!(with_screenshots[0].context_image_count, 2);
    assert!(with_screenshots[0].markdown.contains(&image));
    assert!(!with_screenshots[0]
        .markdown
        .contains("SKILL_INJECTION_MARKER"));
    assert!(with_screenshots[0]
        .original
        .contains("SKILL_INJECTION_MARKER"));
    for remainder in [
        format!("{suffix}\n我的新问题"),
        "\n### 图片 · not-a-timestamp\n".into(),
        format!("\n{image}"),
    ] {
        let mut mixed = message_cards(&format!("## 用户 · second\n{skill}{remainder}"));
        message_metadata::apply(&mut mixed, &root.join("manifest.json"));
        assert!(
            mixed[0].context.is_none(),
            "only complete generated image sections may join a meta message"
        );
    }
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn human_envelopes_require_one_complete_message_and_preserve_trailing_requests() {
    let envelope = "<wake reason=\"mention\"><project id=\"demo\"><message from=\"human\">真实问题</message></project></wake>";
    assert_eq!(
        message_metadata::human_request(envelope).as_deref(),
        Some("真实问题")
    );
    for text in [
        format!("{envelope}\n还有问题"),
        envelope.replace("human", "agent"),
        envelope.replace("</wake>", ""),
        envelope.replace(
            "</message>",
            "</message><message from=\"human\">第二条</message>",
        ),
    ] {
        assert!(message_metadata::human_request(&text).is_none());
        assert_eq!(
            message_cards(&format!("## 用户 · \n{text}"))[0].markdown,
            text
        );
    }
}

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
