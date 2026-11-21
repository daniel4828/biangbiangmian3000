// Zeichnet das App-Symbol von "Biangbiang Quick" (1024×1024 PNG).
// Aufruf: swift make_icon.swift <ausgabe.png>
// Motiv: breite Biangbiang-Nudel als Welle auf Chiliöl-Verlauf, dazu Essstäbchen.
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

// macOS-Raster: Symbolkörper 824×824 mittig, Eckenradius ~185.
let body = CGRect(x: 100, y: 100, width: 824, height: 824)
let shape = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)

// Schlagschatten
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: rgb(0x000000, 0.35).cgColor)
rgb(0xC0261B).setFill()
shape.fill()
ctx.restoreGState()

// Hintergrund: Chiliöl-Verlauf
ctx.saveGState()
shape.addClip()
NSGradient(colors: [rgb(0xFFB23E), rgb(0xF0602A), rgb(0xB8161E)],
           atLocations: [0, 0.5, 1], colorSpace: .sRGB)!
    .draw(in: body, angle: -60)
// Weicher Glanz oben links
NSGradient(colors: [rgb(0xFFFFFF, 0.28), rgb(0xFFFFFF, 0)])!
    .draw(in: NSBezierPath(ovalIn: CGRect(x: 40, y: 520, width: 640, height: 520)), relativeCenterPosition: .zero)

// Breite Nudelbänder: drei Wellen, die von links nach rechts laufen
func noodle(y: CGFloat, amp: CGFloat, phase: CGFloat, width: CGFloat) -> NSBezierPath {
    let p = NSBezierPath()
    let steps = 120
    for i in 0...steps {
        let t = CGFloat(i) / CGFloat(steps)
        let x = 60 + t * 904
        let yy = y + amp * sin(t * .pi * 2.2 + phase)
        if i == 0 { p.move(to: CGPoint(x: x, y: yy)) } else { p.line(to: CGPoint(x: x, y: yy)) }
    }
    p.lineWidth = width
    p.lineCapStyle = .round
    p.lineJoinStyle = .round
    return p
}

let strands: [(CGFloat, CGFloat, CGFloat)] = [(600, 70, 0.0), (460, 80, 0.9), (320, 65, 1.9)]
for (y, amp, phase) in strands {
    let n = noodle(y: y, amp: amp, phase: phase, width: 92)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 18, color: rgb(0x6A0A0A, 0.45).cgColor)
    rgb(0xFFF4DC).setStroke()
    n.stroke()
    ctx.restoreGState()
    // Glanzlinie auf der Nudel
    let hl = noodle(y: y + 22, amp: amp, phase: phase, width: 16)
    rgb(0xFFFFFF, 0.75).setStroke()
    hl.stroke()
}

// Chiliflocken
let flakes: [(CGFloat, CGFloat, CGFloat)] = [(250, 760, 18), (360, 820, 12), (720, 780, 16), (820, 690, 11),
                                             (200, 200, 14), (520, 180, 12), (800, 230, 17), (640, 860, 10)]
for (x, y, r) in flakes {
    rgb(0x8E0F12, 0.85).setFill()
    NSBezierPath(ovalIn: CGRect(x: x - r, y: y - r * 0.7, width: r * 2, height: r * 1.4)).fill()
}

// Essstäbchen (diagonal, oben rechts hinein)
func chopstick(from a: CGPoint, to b: CGPoint) {
    let p = NSBezierPath()
    p.move(to: a); p.line(to: b)
    p.lineWidth = 34
    p.lineCapStyle = .round
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -8), blur: 14, color: rgb(0x000000, 0.35).cgColor)
    rgb(0x2B1A14).setStroke()
    p.stroke()
    ctx.restoreGState()
    let tip = NSBezierPath()
    tip.move(to: a); tip.line(to: CGPoint(x: a.x + (b.x - a.x) * 0.22, y: a.y + (b.y - a.y) * 0.22))
    tip.lineWidth = 34
    tip.lineCapStyle = .round
    rgb(0xE9C98A).setStroke()
    tip.stroke()
}
chopstick(from: CGPoint(x: 1010, y: 1010), to: CGPoint(x: 560, y: 520))
chopstick(from: CGPoint(x: 1060, y: 930), to: CGPoint(x: 600, y: 470))

ctx.restoreGState()

// Feiner Rand
rgb(0xFFFFFF, 0.18).setStroke()
shape.lineWidth = 4
shape.stroke()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
