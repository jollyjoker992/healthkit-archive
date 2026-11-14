import AppKit
import ImageIO
// Final app icon: heart padlock (option B). Light = black glyph on white (opaque, no alpha, as the App
// Store requires); dark = white glyph on transparent (iOS draws the dark background); tinted = white glyph
// on black (iOS tints the grayscale).
// Regenerate: swift ios/tools/app-icon.swift ios/App/Assets.xcassets
let S: CGFloat = 1024
func heart(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat) -> CGMutablePath {
    let p = CGMutablePath()
    func pt(_ a: CGFloat, _ b: CGFloat) -> CGPoint { CGPoint(x: x + a * w, y: y + b * w) }
    p.move(to: pt(0.5, 0.95))
    p.addCurve(to: pt(0, 0.30), control1: pt(0.15, 0.70), control2: pt(0, 0.50))
    p.addCurve(to: pt(0.28, 0), control1: pt(0, 0.12), control2: pt(0.13, 0))
    p.addCurve(to: pt(0.5, 0.16), control1: pt(0.39, 0), control2: pt(0.47, 0.06))
    p.addCurve(to: pt(0.72, 0), control1: pt(0.53, 0.06), control2: pt(0.61, 0))
    p.addCurve(to: pt(1, 0.30), control1: pt(0.87, 0), control2: pt(1, 0.12))
    p.addCurve(to: pt(0.5, 0.95), control1: pt(1, 0.50), control2: pt(0.85, 0.70))
    p.closeSubpath()
    return p
}
func render(_ path: String, bg: CGColor?, glyph: CGColor, alpha: Bool, size: CGFloat = S) {
    let info = alpha ? CGImageAlphaInfo.premultipliedLast.rawValue : CGImageAlphaInfo.noneSkipLast.rawValue
    let c = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8, bytesPerRow: 0,
                      space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: info)!
    c.scaleBy(x: size / S, y: size / S)
    c.translateBy(x: 0, y: S); c.scaleBy(x: 1, y: -1)
    if let bg { c.setFillColor(bg); c.fill(CGRect(x: 0, y: 0, width: S, height: S)) }
    c.setStrokeColor(glyph); c.setLineWidth(70); c.setLineCap(.round)
    let r: CGFloat = 150
    c.move(to: CGPoint(x: S / 2 - r, y: 470)); c.addLine(to: CGPoint(x: S / 2 - r, y: 380))
    c.addArc(center: CGPoint(x: S / 2, y: 380), radius: r, startAngle: .pi, endAngle: 0, clockwise: false)
    c.addLine(to: CGPoint(x: S / 2 + r, y: 470)); c.strokePath()
    let w: CGFloat = 540
    c.addPath(heart((S - w) / 2, 400, w)); c.setFillColor(glyph); c.fillPath()
    let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL, "public.png" as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, c.makeImage()!, nil)
    precondition(CGImageDestinationFinalize(dest))
}
let out = CommandLine.arguments[1]
let white = CGColor(gray: 1, alpha: 1), black = CGColor(gray: 0, alpha: 1)
render(out + "/AppIcon.appiconset/icon-light.png", bg: white, glyph: black, alpha: false)
render(out + "/AppIcon.appiconset/icon-dark.png", bg: nil, glyph: white, alpha: true)
render(out + "/AppIcon.appiconset/icon-tinted.png", bg: black, glyph: white, alpha: false)
// in-app logo (welcome screen): same artwork, rounded in SwiftUI
render(out + "/Logo.imageset/logo.png", bg: white, glyph: black, alpha: false, size: 360)
render(out + "/Logo.imageset/logo-dark.png", bg: CGColor(gray: 0.11, alpha: 1), glyph: white, alpha: false, size: 360)
