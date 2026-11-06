use super::*;

fn session(branch: &str, id: &str, project_id: Option<&str>, cwd: &str) -> Session {
    Session {
        meta: SessionMeta {
            id: id.into(),
            project: "同名项目".into(),
            project_id: project_id.map(str::to_owned),
            cwd: cwd.into(),
            ..Default::default()
        },
        display_title: id.into(),
        branch: branch.into(),
        device: "test".into(),
        path: PathBuf::new(),
        markdown: String::new(),
        search: String::new(),
    }
}

fn node(timestamp: &str, kind: &str, message_index: usize) -> timeline::Node {
    timeline::Node {
        message_index,
        timestamp: timestamp.into(),
        kind: kind.into(),
        title: "原文节点".into(),
        request: "请增加时间线".into(),
        response: "已增加时间线".into(),
    }
}

#[test]
fn timeline_orders_instants_across_timezones_and_keeps_unknown_times_last() {
    let catalog = Catalog {
        sessions: vec![session("mac-codex", "a", None, "/a")],
        timelines: vec![vec![
            node("2026-10-29T10:00:00+08:00", "需求", 0),
            node("2026-10-29T03:00:00Z", "成果", 1),
            node("", "问题", 2),
            node("not-a-date", "决定", 3),
        ]],
        ..Default::default()
    };
    assert_eq!(
        timeline_entries(&catalog, &[0], "", false),
        vec![(0, 0), (0, 1), (0, 2), (0, 3)]
    );
    assert_eq!(
        timeline_entries(&catalog, &[0], "", true),
        vec![(0, 1), (0, 0), (0, 2), (0, 3)]
    );
    assert_eq!(
        timeline_entries(&catalog, &[0], "成果", false),
        vec![(0, 1)]
    );
    assert!(timeline_entries(&catalog, &[0], "修复", false).is_empty());
    assert!(timeline_entries(&catalog, &[], "", false).is_empty());
}

#[test]
fn timeline_ties_are_stable_when_catalog_order_changes() {
    let same_time = "2026-10-29T03:00:00Z";
    let catalog = Catalog {
        sessions: vec![
            session("z-codex", "a", None, "/a"),
            session("a-codex", "b", None, "/a"),
            session("a-codex", "a", None, "/a"),
        ],
        timelines: vec![
            vec![node(same_time, "成果", 0)],
            vec![node(same_time, "成果", 0)],
            vec![
                node(same_time, "需求", 4),
                node("2026-10-29T11:00:00+08:00", "决定", 1),
            ],
        ],
        ..Default::default()
    };
    let expected = vec![(2, 1), (2, 0), (1, 0), (0, 0)];
    for rows in [vec![0, 1, 2], vec![2, 0, 1]] {
        assert_eq!(timeline_entries(&catalog, &rows, "", false), expected);
        assert_eq!(timeline_entries(&catalog, &rows, "", true), expected);
    }
}

#[test]
fn timeline_project_identity_falls_back_from_empty_ids_and_preserves_boundaries() {
    let catalog = Catalog {
        sessions: vec![
            session("mac-codex", "a", Some(""), "/one"),
            session("mac-codex", "b", Some("  "), "/two"),
            session("mac-codex", "c", None, "/one"),
            session("mac-claude", "d", None, "/one"),
            session("mac-codex", "e", Some("project-a"), "/one"),
            session("mac-codex", "f", Some("project-a"), "/one/worktree"),
        ],
        ..Default::default()
    };
    let groups = project_groups(&catalog, &[0, 1, 2, 3, 4, 5]);
    assert_eq!(groups.len(), 4);
    assert!(groups.iter().any(|(_, rows)| rows == &[0, 2]));
    assert!(groups.iter().any(|(_, rows)| rows == &[4, 5]));
    assert_ne!(
        project_identity(&catalog.sessions[0]),
        project_identity(&catalog.sessions[3])
    );
    assert_ne!(
        project_identity(&catalog.sessions[0]),
        project_identity(&catalog.sessions[4])
    );
}

#[test]
fn timeline_jump_renders_and_scrolls_to_an_offscreen_original_message() {
    let markdown = (0..100)
        .map(|index| {
            format!(
                "## 用户 · 2026-10-29T03:00:00Z\n\nMessage {index}. {}\n\n",
                "Details. ".repeat(if index % 5 == 0 { 100 } else { 2 })
            )
        })
        .collect::<String>();
    let ctx = egui::Context::default();
    let mut cards = message_cards(&markdown);
    let mut cache = CommonMarkCache::default();
    let target = cards.len() - 1;
    let mut scroll_target = Some(target);
    for frame in 0..120 {
        let _ = ctx.run(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    egui::vec2(700.0, 500.0),
                )),
                time: Some(frame as f64 / 60.0),
                ..Default::default()
            },
            |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| {
                    show_message_cards(
                        ui,
                        &mut cards,
                        &mut cache,
                        320,
                        Some(target),
                        &mut scroll_target,
                    );
                });
            },
        );
        assert!(
            cards[target].size.x > 0.0,
            "the target must bypass virtualized offscreen skipping"
        );
        if scroll_target.is_none() {
            break;
        }
    }
    assert_eq!(
        scroll_target, None,
        "jump should settle once the original message is visible"
    );
}
