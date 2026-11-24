#!/usr/bin/env swift
import AppKit
import Foundation

/// CoreGraphics 矢量绘制 App 图标主图（抗锯齿），输出 PNG 路径由 argv[1] 指定。
let outPath = CommandLine.arguments.count > 1
  ? CommandLine.arguments[1]
  : "/tmp/cna-app-icon-1024.png"
let size: CGFloat = 1024

guard let rep = NSBitmapImageRep(
  bitmapDataPlanes: nil,
  pixelsWide: Int(size),
  pixelsHigh: Int(size),
  bitsPerSample: 8,
  samplesPerPixel: 4,
  hasAlpha: true,
  isPlanar: false,
  colorSpaceName: .deviceRGB,
  bytesPerRow: 0,
  bitsPerPixel: 0
) else {
  fputs("bitmap failed\n", stderr)
  exit(1)
}
rep.size = NSSize(width: size, height: size)

NSGraphicsContext.saveGraphicsState()
guard let gc = NSGraphicsContext(bitmapImageRep: rep) else {
  fputs("context failed\n", stderr)
  exit(1)
}
NSGraphicsContext.current = gc
defer { NSGraphicsContext.restoreGraphicsState() }

let ctx = gc.cgContext
ctx.setAllowsAntialiasing(true)
ctx.setShouldAntialias(true)
ctx.interpolationQuality = .high
// Bitmap 默认 y 向上；先翻成「顶为 0」方便按设计坐标画
ctx.translateBy(x: 0, y: size)
ctx.scaleBy(x: 1, y: -1)
ctx.clear(CGRect(x: 0, y: 0, width: size, height: size))

let margin = size * 0.06
let box = CGRect(x: margin, y: margin, width: size - 2 * margin, height: size - 2 * margin)
let radius = box.width * 0.22
let round = CGPath(
  roundedRect: box,
  cornerWidth: radius,
  cornerHeight: radius,
  transform: nil
)

let c0 = NSColor(calibratedRed: 32 / 255, green: 148 / 255, blue: 138 / 255, alpha: 1).cgColor
let c1 = NSColor(calibratedRed: 10 / 255, green: 100 / 255, blue: 94 / 255, alpha: 1).cgColor
guard let grad = CGGradient(
  colorsSpace: CGColorSpaceCreateDeviceRGB(),
  colors: [c0, c1] as CFArray,
  locations: [0, 1]
) else {
  fputs("gradient failed\n", stderr)
  exit(1)
}
ctx.saveGState()
ctx.addPath(round)
ctx.clip()
ctx.drawLinearGradient(
  grad,
  start: CGPoint(x: box.midX, y: box.minY),
  end: CGPoint(x: box.midX, y: box.maxY),
  options: []
)
ctx.restoreGState()

let cx = size / 2
let cy = size / 2
let ringR = size * 0.285
let stroke = size * 0.046
ctx.setStrokeColor(NSColor.white.cgColor)
ctx.setLineWidth(stroke)
ctx.setLineCap(.round)
ctx.setLineJoin(.round)
ctx.strokeEllipse(in: CGRect(x: cx - ringR, y: cy - ringR, width: ringR * 2, height: ringR * 2))

// 闪电（设计坐标：y 向下为正）
let pts: [(CGFloat, CGFloat)] = [
  (-0.16, -0.58),
  (0.30, -0.58),
  (0.04, -0.06),
  (0.40, -0.06),
  (-0.14, 0.66),
  (0.10, 0.06),
  (-0.34, 0.06),
  (0.00, -0.26),
  (-0.40, -0.26),
]
let r = ringR * 0.92
ctx.beginPath()
for (i, p) in pts.enumerated() {
  let pt = CGPoint(x: cx + p.0 * r, y: cy + p.1 * r)
  if i == 0 { ctx.move(to: pt) } else { ctx.addLine(to: pt) }
}
ctx.closePath()
ctx.setFillColor(NSColor.white.cgColor)
ctx.fillPath()

guard let png = rep.representation(using: .png, properties: [:]) else {
  fputs("png failed\n", stderr)
  exit(1)
}
do {
  try png.write(to: URL(fileURLWithPath: outPath))
  fputs("wrote \(outPath)\n", stderr)
} catch {
  fputs("write failed: \(error)\n", stderr)
  exit(1)
}
