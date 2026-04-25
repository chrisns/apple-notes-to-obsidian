// Decode an Apple Notes ZMERGEABLEDATA1 blob (PKDrawing data) into JSON.
// Usage: swift decode_pkdrawing.swift <input.bin> > <output.json>
//
// Emits:
//   {
//     "bounds": {"x": ..., "y": ..., "w": ..., "h": ...},
//     "strokes": [
//       {
//         "ink": {"type": "pen", "color": "#000000FF", "width": 1.0},
//         "transform": [a, b, c, d, tx, ty],
//         "path": [
//           {"x": ..., "y": ..., "t": ..., "force": ..., "azimuth": ..., "altitude": ..., "size": [w, h], "opacity": ...},
//           ...
//         ]
//       }
//     ]
//   }

import Foundation
import PencilKit
import CoreGraphics
import AppKit

func hexColor(_ color: NSColor) -> String {
	guard let rgb = color.usingColorSpace(.sRGB) else {
		return "#000000FF"
	}
	let r = Int(round(rgb.redComponent * 255))
	let g = Int(round(rgb.greenComponent * 255))
	let b = Int(round(rgb.blueComponent * 255))
	let a = Int(round(rgb.alphaComponent * 255))
	return String(format: "#%02X%02X%02X%02X", r, g, b, a)
}

// On macOS PencilKit exposes the type via PKInkingTool.InkType. Stringify by
// reflection so we don't have to keep up with new cases.
func inkTypeName(_ ink: PKInk) -> String {
	return String(describing: ink.inkType)
}

guard CommandLine.arguments.count == 2 else {
	FileHandle.standardError.write("usage: decode_pkdrawing.swift <input>\n".data(using: .utf8)!)
	exit(2)
}

let path = CommandLine.arguments[1]
let data = try Data(contentsOf: URL(fileURLWithPath: path))

let drawing: PKDrawing
do {
	drawing = try PKDrawing(data: data)
} catch {
	FileHandle.standardError.write("PKDrawing(data:) failed: \(error)\n".data(using: .utf8)!)
	exit(1)
}

let b = drawing.bounds
var out: [String: Any] = [
	"bounds": [
		"x": b.origin.x,
		"y": b.origin.y,
		"w": b.size.width,
		"h": b.size.height,
	]
]

var strokes: [[String: Any]] = []
for stroke in drawing.strokes {
	var points: [[String: Any]] = []
	for pt in stroke.path {
		points.append([
			"x": pt.location.x,
			"y": pt.location.y,
			"t": pt.timeOffset,
			"force": pt.force,
			"azimuth": pt.azimuth,
			"altitude": pt.altitude,
			"size": [pt.size.width, pt.size.height],
			"opacity": pt.opacity,
		])
	}
	let t = stroke.transform
	strokes.append([
		"ink": [
			"type": inkTypeName(stroke.ink),
			"color": hexColor(stroke.ink.color),
		],
		"transform": [t.a, t.b, t.c, t.d, t.tx, t.ty],
		"renderBounds": [
			"x": stroke.renderBounds.origin.x,
			"y": stroke.renderBounds.origin.y,
			"w": stroke.renderBounds.size.width,
			"h": stroke.renderBounds.size.height,
		],
		"path": points,
	])
}
out["strokes"] = strokes

let json = try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(json)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
