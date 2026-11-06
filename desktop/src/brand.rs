use eframe::egui::{self, Color32, Response, Sense, Shape, Stroke, Ui};

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
