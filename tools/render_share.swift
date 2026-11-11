// Reproducible promotional layouts using fixed examples, never user conversations.
import AppKit
let base = URL(fileURLWithPath: CommandLine.arguments[1])
let catalog = try JSONSerialization.jsonObject(with: Data(contentsOf:base.appendingPathComponent("share/catalog.json"))) as! [String:Any]
func color(_ hex:UInt32, _ alpha:CGFloat=1)->NSColor { NSColor(srgbRed:CGFloat((hex>>16)&255)/255,green:CGFloat((hex>>8)&255)/255,blue:CGFloat(hex&255)/255,alpha:alpha) }
let navy=color(0x14213b), gold=color(0xf4cc7c), ink=color(0x29354b), muted=color(0x73819b), paper=color(0xf2f5fc)
func rect(_ x:CGFloat,_ y:CGFloat,_ w:CGFloat,_ h:CGFloat,_ fill:NSColor,_ radius:CGFloat=0) {
    fill.setFill(); NSBezierPath(roundedRect:NSRect(x:x,y:y,width:w,height:h),xRadius:radius,yRadius:radius).fill()
}
func text(_ value:String,_ x:CGFloat,_ y:CGFloat,_ w:CGFloat,_ h:CGFloat,_ size:CGFloat,_ fill:NSColor,_ weight:NSFont.Weight = .regular) {
    let paragraph=NSMutableParagraphStyle(); paragraph.lineSpacing=5; paragraph.lineBreakMode = .byWordWrapping
    var fontSize=size
    var attributed=NSAttributedString()
    repeat {
        attributed=NSAttributedString(string:value,attributes:[.font:NSFont.systemFont(ofSize:fontSize,weight:weight),.foregroundColor:fill,.paragraphStyle:paragraph])
        let measured=attributed.boundingRect(with:NSSize(width:w,height:10000),options:[.usesLineFragmentOrigin,.usesFontLeading])
        if measured.height <= h { break }; fontSize -= 1
    } while fontSize > 12
    attributed.draw(with:NSRect(x:x,y:y,width:w,height:h),options:[.usesLineFragmentOrigin,.usesFontLeading])
}
func line(_ points:[NSPoint],_ fill:NSColor,_ width:CGFloat=2) {
    let p=NSBezierPath();p.move(to:points[0]);for point in points.dropFirst(){p.line(to:point)};p.lineWidth=width;p.lineCapStyle = .round;fill.setStroke();p.stroke()
}
func badge(_ title:String,_ x:CGFloat,_ y:CGFloat,_ w:CGFloat,_ dark:Bool=false) {
    rect(x,y,w,42,dark ? color(0xffffff,0.1):paper,13)
    text(title,x+14,y+9,w-28,28,19,dark ? color(0xe5edff):ink,.medium)
}
for item in catalog["images"] as! [[String:Any]] {
    let id=item["id"] as! String, english=(item["locale"] as! String)=="en-US"
    let kind=id.replacingOccurrences(of:"-en",with:"")
    let rep=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:1080,pixelsHigh:1440,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0)!
    NSGraphicsContext.saveGraphicsState()
    let graphics=NSGraphicsContext(bitmapImageRep:rep)!
    graphics.cgContext.translateBy(x:0,y:1440);graphics.cgContext.scaleBy(x:1,y:-1)
    NSGraphicsContext.current=NSGraphicsContext(cgContext:graphics.cgContext,flipped:true)
    rect(0,0,1080,1440,navy)
    NSGradient(starting:color(0x223b66),ending:color(0x111a2f))!.draw(in:NSBezierPath(rect:NSRect(x:0,y:0,width:1080,height:1440)),angle:-40)
    for i in 0..<70 {
        let x=CGFloat((i*173+41)%1080), y=CGFloat((i*293+71)%1440), d:CGFloat=i%7==0 ? 3:1.5
        color(0xd7e5ff,i%7==0 ? 0.5:0.18).setFill();NSBezierPath(ovalIn:NSRect(x:x,y:y,width:d,height:d)).fill()
    }
    let icon=NSImage(contentsOf:base.appendingPathComponent("starvault-256.png"))!
    icon.draw(in:NSRect(x:65,y:53,width:72,height:72))
    text(english ? "StarVault":"星匣 StarVault",150,58,650,44,31,color(0xffffff),.semibold)
    text(english ? "YOUR AI CONVERSATION ARCHIVE":"AI 对话档案 · 本地优先",151,105,800,28,16,color(0xaebddd),.medium)
    text(item["headline"] as! String,72,192,936,240,english ? 84:82,color(0xffffff),.bold)
    text(item["subtitle"] as! String,74,432,930,64,27,color(0xb8c8e6))
    if kind=="overview" {
        rect(72,540,936,475,color(0xffffff),24)
        rect(72,540,936,64,color(0x233557),24);rect(72,573,936,31,color(0x233557))
        text(english ? "StarVault · Conversations":"星匣 · 会话档案",98,556,700,32,22,color(0xffffff),.medium)
        rect(72,604,270,411,paper,0)
        text(english ? "Your conversations":"你的对话",96,628,220,32,20,ink,.semibold)
        let titles=english ? ["Project decisions","Research notes","Saved ideas","Migration checklist"]:["项目方案讨论","研究笔记","值得保留的灵感","换机迁移清单"]
        for (i,title) in titles.enumerated() {
            let y=CGFloat(688+i*71)
            if i==0 {rect(86,y-8,242,63,color(0xe0e6fb),10)}
            text(title,100,y,223,26,17,ink,.medium)
            text(["Codex","Claude Code","WorkBuddy","OpenClaw"][i],100,y+29,223,23,13,muted)
        }
        text(english ? "Project decisions":"项目方案讨论",373,634,585,40,29,ink,.semibold)
        badge(english ? "Original conversation":"原始对话",374,687,english ? 230:160)
        rect(370,754,604,82,color(0xebeffb),14)
        text(english ? "USER · How should we move this project forward?":"用户 · 这个项目接下来应该怎么推进？",388,773,563,45,21,ink)
        text(english ? "ASSISTANT · We can work through three stages, starting with the prototype.":"助手 · 可以分三个阶段推进，先从原型开始。",388,868,555,85,22,ink)
        text(english ? "Illustration · Fixed sample content":"功能示意 · 原创固定样例",373,978,590,24,14,muted)
    } else if kind=="ownership" {
        rect(72,540,936,475,color(0xffffff),24)
        text(english ? "Your complete archive":"完整备份目录",110,580,800,50,33,ink,.semibold)
        let names=english ? ["Conversations","Backed-up files","Skills & versions"]:["对话原文与索引","已备份的图片与文件","Skill 与历史版本"]
        for (i,name) in names.enumerated() {
            let y=CGFloat(655+i*77)
            rect(110,y,860,61,paper,12);text(String(format:"%02d",i+1),129,y+14,60,35,24,color(0x5e72ab),.medium)
            text(name,207,y+15,720,34,25,ink,.medium)
        }
        line([NSPoint(x:210,y:924),NSPoint(x:865,y:924)],color(0x8da1ca),3)
        line([NSPoint(x:853,y:914),NSPoint(x:867,y:924),NSPoint(x:853,y:934)],color(0x8da1ca),3)
        text(english ? "OLD COMPUTER":"旧电脑",113,957,340,35,21,muted,.medium)
        text(english ? "NEXT COMPUTER":"新电脑",742,957,270,35,21,muted,.medium)
    } else {
        rect(72,540,936,475,color(0x0b1427),24)
        text(english ? "FIND CONTEXT · READ THE ORIGINAL":"查找历史 · 核对原文",106,574,860,32,20,gold,.medium)
        let command=english ? "$ starvault search \"project decisions\"\n\nFound in your archive\nProject plan / Original conversation\n\n→ Read the source before continuing.":"$ starvault search \"项目决定\"\n\n在本地档案中找到\n项目方案 / 原始对话\n\n→ 先核对原文，再继续这次任务。"
        text(command,110,643,860,260,25,color(0xdbe5fa),.medium)
        badge("MCP",109,928,150,true);badge("CLI",284,928,150,true);badge("Skill",459,928,150,true)
        text(english ? "Illustration · No built-in AI model":"功能示意 · 星匣不内置 AI 模型",110,986,850,23,14,color(0xaabbdc))
    }
    let features=item["features"] as! [[String]]
    for (i,feature) in features.enumerated() {
        let x=CGFloat(72+i*316)
        rect(x,1060,296,148,color(0xffffff,0.065),18)
        text(feature[0],x+20,1083,256,36,25,gold,.semibold)
        text(feature[1],x+20,1130,256,63,english ? 20:22,color(0xd9e3f7))
    }
    text("github.com/StarDreamTech/starvault",74,1260,935,37,26,color(0xffffff),.medium)
    let note:String
    if kind=="overview" {note=english ? "Local sources + manual imports · Cache coverage may be partial":"本地来源与手动导入 · 缓存覆盖可能不完整"}
    else if kind=="ownership" {note=english ? "Move the complete folder. Only files already backed up travel with it.":"请复制整个备份目录；尚未备份的原文件需先补齐。"}
    else {note=english ? "Your connected client decides how to use the archived context.":"提供已归档的历史资料；使用方式由接入的 AI 客户端决定。"}
    text(note,74,1311,935,45,18,color(0xa8b9d7))
    text(english ? "Desktop installer: Apple Silicon macOS · CLI: macOS / Windows / Linux":"桌面安装包：Apple Silicon macOS · CLI：macOS / Windows / Linux",74,1370,940,24,14,color(0x8da2c8))
    NSGraphicsContext.restoreGraphicsState()
    let png=rep.representation(using:.png,properties:[:])!
    try png.write(to:base.appendingPathComponent("share/\(id).png"))
    print("\(id): \(png.count) bytes")
}
