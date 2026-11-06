// Native vector rendering of assets/starvault.svg, no external asset service.
import AppKit
let output = CommandLine.arguments[1]
let size = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let context = NSGraphicsContext.current!.cgContext
context.translateBy(x: 0, y: 1024)
context.scaleBy(x: 1, y: -1)
func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(red: r/255, green: g/255, blue: b/255, alpha: a) }
let background = CGPath(roundedRect: CGRect(x:64,y:64,width:896,height:896), cornerWidth:204, cornerHeight:204, transform:nil)
context.saveGState()
context.addPath(background); context.clip()
let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: [color(44,64,112),color(17,27,50)] as CFArray, locations:[0,1])!
context.drawLinearGradient(gradient, start:CGPoint(x:100,y:80),end:CGPoint(x:900,y:940),options:[])
context.restoreGState()
context.addPath(CGPath(roundedRect:CGRect(x:84,y:84,width:856,height:856),cornerWidth:188,cornerHeight:188,transform:nil))
context.setStrokeColor(color(129,155,209,0.2)); context.setLineWidth(3); context.strokePath()
func path(_ points: [(CGFloat, CGFloat)], close: Bool = false) -> CGPath {
    let p = CGMutablePath(); p.move(to:CGPoint(x:points[0].0,y:points[0].1))
    for pt in points.dropFirst() { p.addLine(to:CGPoint(x:pt.0,y:pt.1)) }
    if close { p.closeSubpath() }; return p
}
context.saveGState()
context.addPath(path([(512,198),(553,390),(724,431),(553,472),(512,655),(471,472),(300,431),(471,390)],close:true)); context.clip()
context.drawLinearGradient(CGGradient(colorsSpace:CGColorSpaceCreateDeviceRGB(),colors:[color(255,231,170),color(234,184,108)] as CFArray,locations:[0,1])!,start:CGPoint(x:400,y:200),end:CGPoint(x:650,y:650),options:[])
context.restoreGState()
context.setLineCap(.round); context.setLineJoin(.round); context.setLineWidth(30); context.setStrokeColor(color(165,186,229))
context.addPath(path([(249,637),(315,789),(709,789),(775,637)])); context.strokePath()
context.setLineWidth(10); context.setStrokeColor(color(165,186,229,0.3)); context.addPath(path([(346,697),(678,697)])); context.strokePath()
context.setFillColor(color(255,225,160)); context.fillEllipse(in:CGRect(x:737,y:257,width:26,height:26))
context.setFillColor(color(165,186,229)); context.fillEllipse(in:CGRect(x:269,y:318,width:12,height:12))
NSGraphicsContext.restoreGraphicsState()
try rep.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:output))
