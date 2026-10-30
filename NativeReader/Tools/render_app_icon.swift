// Native rendition of the existing Docker reader's app-icon.svg.
// iOS applies the icon mask; the source remains an opaque square.
import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
let size = 1024
let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: size * 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
context.setFillColor(CGColor(red: 23/255, green: 25/255, blue: 29/255, alpha: 1))
context.fill(CGRect(x: 0, y: 0, width: size, height: size))
context.scaleBy(x: 1024/192, y: 1024/192)
context.translateBy(x: 0, y: 192); context.scaleBy(x: 1, y: -1)
context.setStrokeColor(CGColor(red: 200/255, green: 216/255, blue: 198/255, alpha: 1))
context.setLineWidth(8)
context.move(to: CGPoint(x: 40, y: 48)); context.addLine(to: CGPoint(x: 88, y: 48))
for p in [(96,56),(104,48),(152,48),(152,146),(104,146),(96,154),(88,146),(40,146)] { context.addLine(to: CGPoint(x:p.0,y:p.1)) }
context.closePath(); context.strokePath(); context.setLineWidth(6)
for line in [(96,56,96,150),(54,76,80,76),(54,96,80,96),(112,76,138,76),(112,96,138,96)] {
    context.move(to: CGPoint(x:line.0,y:line.1));context.addLine(to: CGPoint(x:line.2,y:line.3))
}
context.strokePath()
let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, context.makeImage()!, nil)
precondition(CGImageDestinationFinalize(destination))
