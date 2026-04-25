// Decode an Apple Notes Paper bundle into the same JSON shape that
// decode_pkdrawing.swift emits, so ink_writer.build_writing_from_strokes can
// consume it unchanged.
//
// Strategy: open the bundle via Coherence (with our shim) → Capsule<Paper>.
// Walk the Capsule's allReferences (TreeDictionary<CRKeyPath, CapsuleReference>)
// via Mirror to find each PKStrokeStruct, resolve its path (PencilKit.PKStrokePath)
// and ink (PKInk). For each stroke, emit:
//   { "ink": {"type", "color"}, "transform": [a,b,c,d,tx,ty],
//     "renderBounds": {x,y,w,h}, "path": [{x,y,t,force,azimuth,altitude,size,opacity}] }
//
// Usage: decode_paper <bundle-path> > out.json

import Foundation
import Coherence
import PaperKit
import PencilKit
import CoreGraphics
import AppKit

setbuf(stdout, nil)

// ---- Mirror helpers ----

func child(_ value: Any, _ label: String) -> Any? {
	for (l, c) in Mirror(reflecting: value).children where l == label { return c }
	return nil
}

func childContaining(_ value: Any, _ substring: String) -> Any? {
	for (l, c) in Mirror(reflecting: value).children {
		if let l, l.contains(substring) { return c }
	}
	return nil
}

func unwrapOptional(_ value: Any) -> Any? {
	let m = Mirror(reflecting: value)
	if m.displayStyle == .optional { return m.children.first?.value }
	return value
}

/// Pull the inner CRDT instance from a CapsuleReference (the value side of the
/// allReferences dict). Returns the typed payload (e.g. PKStrokeStruct).
func payload(of capsuleReference: Any) -> Any? {
	guard
		let crdtOpt = child(capsuleReference, "crdt"),
		let anyCRDT = unwrapOptional(crdtOpt),
		let box = child(anyCRDT, "box"),
		let inner = child(box, "crdt"),
		let unwrapped = unwrapOptional(inner)
	else { return nil }
	return unwrapped
}

/// Pull the value out of a `Coherence.CRRegister<T>` (its current ref's _value),
/// or return the ref unchanged if it's not a register-like.
func registerValue(_ value: Any) -> Any? {
	// CRRegister is `CRRegister[X]` — Mirror exposes `ref: Optional<CRRegisterRef<T>>`
	// containing `_value`. Try that path; fall back to other shapes.
	if let refOpt = child(value, "ref"), let ref = unwrapOptional(refOpt),
	   let v = child(ref, "_value") {
		return unwrapOptional(v) ?? v
	}
	return value
}

/// Resolve a `Coherence.Ref<T>` to its target by reading `id` and looking it up
/// in the allReferences dictionary, then extracting the inner payload.
func resolveRef(_ ref: Any, allRefs: [AnyHashable: Any]) -> Any? {
	guard let id = child(ref, "id") else { return nil }
	// id is a CRKeyPath. We need to find a key in allRefs whose Mirror equals it.
	// Easiest: stringify and compare; CRKeyPath's description includes its hex.
	let target = String(describing: id)
	for (k, v) in allRefs {
		if String(describing: k) == target {
			return payload(of: v)
		}
	}
	return nil
}

// ---- Build the allReferences map as a Swift dict for fast lookup ----

guard CommandLine.arguments.count >= 2 else {
	FileHandle.standardError.write("usage: decode_paper <bundle-path>\n".data(using: .utf8)!)
	exit(2)
}
let bundlePath = CommandLine.arguments[1]

let ctx = CRContext.newTransientContext(uniqueAssetManager: false, encryptionDelegate: nil)
let url = URL(fileURLWithPath: bundlePath)

let capsule: Capsule<Paper> = try CRDataStoreBundle<Paper>.read(
	ctx, url: url,
	fileVersionPolicy: .all,
	allowedEncodings: [.version1, .version2, .version3, .version4],
	allowedAppFormats: [0, 1, 2, 3, 4, 5]
)

guard
	let cr = child(capsule, "capsuleReference"),
	let capRef = unwrapOptional(cr),
	let refsContainer = child(capRef, "references"),
	let allRefsAny = child(refsContainer, "allReferences")
else {
	FileHandle.standardError.write("could not reach allReferences\n".data(using: .utf8)!)
	exit(1)
}

// Build a String-keyed lookup table: stringified CRKeyPath → CapsuleReference.
// (The native TreeDictionary type is private and not directly subscriptable.)
var allRefs: [String: Any] = [:]
for (_, kv) in Mirror(reflecting: allRefsAny).children {
	let parts = Array(Mirror(reflecting: kv).children)
	guard parts.count == 2 else { continue }
	let key = parts[0].value
	let val = parts[1].value
	allRefs[String(describing: key)] = val
}

func resolveByRef(_ ref: Any) -> Any? {
	guard let id = child(ref, "id") else { return nil }
	if let cap = allRefs[String(describing: id)] { return payload(of: cap) }
	return nil
}

// ---- Helpers to read concrete fields ----

func cgFloat(_ v: Any) -> CGFloat {
	if let d = v as? CGFloat { return d }
	if let d = v as? Double { return CGFloat(d) }
	if let d = v as? Float { return CGFloat(d) }
	if let d = v as? Int { return CGFloat(d) }
	return 0
}

func hexColor(_ color: NSColor) -> String {
	guard let rgb = color.usingColorSpace(.sRGB) else { return "#000000FF" }
	let r = Int(round(rgb.redComponent * 255))
	let g = Int(round(rgb.greenComponent * 255))
	let b = Int(round(rgb.blueComponent * 255))
	let a = Int(round(rgb.alphaComponent * 255))
	return String(format: "#%02X%02X%02X%02X", r, g, b, a)
}

func inkTypeName(_ ink: PKInk) -> String {
	String(describing: ink.inkType)
}

// ---- Walk all PKStrokeStruct records, emit JSON ----

var strokesJSON: [[String: Any]] = []
var paperBounds: CGRect = .zero

for (_, capRefVal) in allRefs {
	guard let p = payload(of: capRefVal) else { continue }

	// Capture the Paper root's bounds for the JSON header
	if String(reflecting: type(of: p)).hasSuffix("PaperKit.Paper"),
	   let boundsRegOpt = child(p, "_bounds"),
	   let bv = registerValue(boundsRegOpt),
	   let rect = bv as? CGRect {
		paperBounds = rect
	}

	guard String(reflecting: type(of: p)).contains("PKStrokeStruct") else { continue }

	// _inherited is a CRRegister<Ref<PKStrokeInheritedProperties>>
	var ink: PKInk? = nil
	var transform = CGAffineTransform.identity
	if let inheritedReg = child(p, "_inherited"),
	   let inheritedRefAny = registerValue(inheritedReg),
	   let inheritedRef = unwrapOptional(inheritedRefAny),
	   let inherited = resolveByRef(inheritedRef) {
		// inherited is PKStrokeInheritedProperties: _ink, _transform are CRRegisters
		if let inkReg = child(inherited, "_ink"),
		   let inkVal = registerValue(inkReg),
		   let inkUnwrapped = unwrapOptional(inkVal),
		   let pkInk = inkUnwrapped as? PKInk {
			ink = pkInk
		}
		if let trReg = child(inherited, "_transform"),
		   let trVal = registerValue(trReg),
		   let aff = trVal as? CGAffineTransform {
			transform = aff
		}
	}

	// _properties is a CRRegister<PKStrokeProperties>
	// PKStrokeProperties has `path: Ref<CRRegister<PKStrokePathStruct>>`
	var pkStrokePath: PKStrokePath? = nil
	if let propsReg = child(p, "_properties"),
	   let propsAny = registerValue(propsReg),
	   let pathRefAny = child(propsAny, "path") {
		// pathRefAny is a Ref<CRRegister<PKStrokePathStruct>> — resolve once to
		// get a CRRegister, then read its value to get PKStrokePathStruct.
		if let pathReg = resolveByRef(pathRefAny),
		   let pathStruct = registerValue(pathReg),
		   let realStruct = (pathStruct as Any?).flatMap({ child($0, "path") }) {
			// realStruct is PencilKit.PKStrokePath (typed)
			if let p = realStruct as? PKStrokePath { pkStrokePath = p }
		}
	}

	guard let pkStrokePath else { continue }

	// PKStrokePath stores compressed *control* points; iterating it directly
	// gives sparse output that tldraw's freehand renderer turns into chunky
	// outlines. Oversample by interpolating at parametric stride 0.5 over the
	// full path — same algorithm Apple uses internally for rendering.
	var points: [[String: Any]] = []
	if pkStrokePath.count >= 2 {
		for pt in pkStrokePath.interpolatedPoints(by: .parametricStep(0.5)) {
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
	} else {
		for pt in pkStrokePath {
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
	}

	let inkType = ink.map(inkTypeName) ?? "pen"
	let inkColor = ink.map { hexColor($0.color) } ?? "#000000FF"

	// Compute renderBounds from points (PencilKit's own renderBounds is on PKStroke,
	// not exposed on the path alone — derive from extents).
	var minX = CGFloat.infinity, minY = CGFloat.infinity
	var maxX = -CGFloat.infinity, maxY = -CGFloat.infinity
	for pt in pkStrokePath {
		minX = min(minX, pt.location.x)
		minY = min(minY, pt.location.y)
		maxX = max(maxX, pt.location.x)
		maxY = max(maxY, pt.location.y)
	}
	let rb: [String: CGFloat] = points.isEmpty
		? ["x": 0, "y": 0, "w": 0, "h": 0]
		: ["x": minX, "y": minY, "w": maxX - minX, "h": maxY - minY]

	strokesJSON.append([
		"ink": ["type": inkType, "color": inkColor],
		"transform": [transform.a, transform.b, transform.c, transform.d, transform.tx, transform.ty],
		"renderBounds": rb,
		"path": points,
	])
}

let result: [String: Any] = [
	"bounds": [
		"x": paperBounds.origin.x,
		"y": paperBounds.origin.y,
		"w": paperBounds.size.width,
		"h": paperBounds.size.height,
	],
	"strokes": strokesJSON,
]

let json = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(json)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)

FileHandle.standardError.write("strokes emitted: \(strokesJSON.count)\n".data(using: .utf8)!)
exit(0)
