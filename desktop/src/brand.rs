use eframe::egui::{self, Color32, Response, Sense, Shape, Stroke, Ui};

pub fn github_link(ui: &mut Ui) -> Response {
    let (rect, response) = ui.allocate_exact_size(egui::vec2(32.0, 32.0), Sense::click());
    let visuals = ui.style().interact(&response);
    if response.hovered() || response.has_focus() {
        ui.painter().rect_filled(rect, 7.0, visuals.weak_bg_fill);
        if response.has_focus() {
            ui.painter()
                .rect_stroke(rect, 7.0, visuals.fg_stroke, egui::StrokeKind::Inside);
        }
    }
    let origin = rect.center() - egui::vec2(9.0, 9.0);
    let point = |(horizontal, vertical)| origin + egui::vec2(horizontal, vertical) * 0.75;
    let stroke = Stroke::new(1.2, visuals.fg_stroke.color);
    for points in [
        [(9.0, 18.4), (9.0, 17.5), (9.3, 16.7), (9.8, 16.2)],
        [(9.8, 16.2), (6.5, 15.8), (3.0, 14.5), (3.0, 9.0)],
        [(3.0, 9.0), (3.0, 7.5), (3.6, 6.2), (4.6, 5.2)],
        [(4.6, 5.2), (4.3, 4.1), (4.4, 2.8), (4.8, 2.0)],
        [(4.8, 2.0), (6.2, 1.9), (8.1, 3.1), (8.6, 3.5)],
        [(8.6, 3.5), (10.8, 3.0), (13.2, 3.0), (15.4, 3.5)],
        [(15.4, 3.5), (15.9, 3.1), (17.8, 1.9), (19.2, 2.0)],
        [(19.2, 2.0), (19.6, 2.8), (19.7, 4.1), (19.4, 5.2)],
        [(19.4, 5.2), (20.4, 6.2), (21.0, 7.5), (21.0, 9.0)],
        [(21.0, 9.0), (21.0, 14.5), (17.5, 15.8), (14.2, 16.2)],
        [(14.2, 16.2), (14.7, 16.7), (15.0, 17.5), (15.0, 18.4)],
        [(9.0, 19.0), (4.0, 20.5), (4.0, 16.5), (2.0, 16.0)],
    ] {
        ui.painter()
            .add(egui::epaint::CubicBezierShape::from_points_stroke(
                points.map(point),
                false,
                Color32::TRANSPARENT,
                stroke,
            ));
    }
    for horizontal in [9.0, 15.0] {
        ui.painter().line_segment(
            [point((horizontal, 18.4)), point((horizontal, 22.0))],
            stroke,
        );
    }
    response.widget_info(|| {
        egui::WidgetInfo::labeled(egui::WidgetType::Link, true, "查看开源项目（GitHub）")
    });
    if response.clicked() {
        ui.ctx()
            .open_url(egui::OpenUrl::new_tab(super::share::PROJECT));
    }
    response
        .on_hover_cursor(egui::CursorIcon::PointingHand)
        .on_hover_text("查看开源项目（GitHub）")
}

/// A small star held in an open vault; scales without image or font dependencies.
pub fn mark(ui: &mut Ui, size: f32) -> Response {
    let (rect, response) = ui.allocate_exact_size(egui::vec2(size, size), Sense::hover());
    let painter = ui.painter();
    let point = |x, y| rect.min + egui::vec2(x * size, y * size);
    let gold = Color32::from_rgb(244, 204, 124);
    painter.rect_filled(rect, size * 0.25, Color32::from_rgb(36, 52, 86));
    // Two convex diamonds form the four-point star without a concave fill path.
    painter.add(Shape::convex_polygon(
        vec![
            point(0.50, 0.16),
            point(0.56, 0.39),
            point(0.50, 0.62),
            point(0.44, 0.39),
        ],
        gold,
        Stroke::NONE,
    ));
    painter.add(Shape::convex_polygon(
        vec![
            point(0.28, 0.39),
            point(0.50, 0.33),
            point(0.72, 0.39),
            point(0.50, 0.45),
        ],
        gold,
        Stroke::NONE,
    ));
    painter.add(Shape::line(
        vec![
            point(0.23, 0.63),
            point(0.30, 0.78),
            point(0.70, 0.78),
            point(0.77, 0.63),
        ],
        Stroke::new(size * 0.035, Color32::from_rgb(155, 177, 221)),
    ));
    painter.circle_filled(point(0.76, 0.23), size * 0.018, gold);
    response.on_hover_text("星匣 · StarVault")
}
