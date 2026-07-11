import AppKit
import Foundation
import Vision

struct Block: Codable {
    let text: String
    let confidence: Float
    let x: CGFloat
    let y: CGFloat
    let width: CGFloat
    let height: CGFloat
}

struct Output: Codable {
    let blocks: [Block]
}

func fail(_ message: String) -> Never {
    if let data = (message + "\n").data(using: .utf8) {
        FileHandle.standardError.write(data)
    }
    exit(EXIT_FAILURE)
}

guard CommandLine.arguments.count == 2 else {
    fail("image path is required")
}

guard
    let image = NSImage(contentsOfFile: CommandLine.arguments[1]),
    let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
else {
    fail("image could not be read")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "en-US"]

do {
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
} catch {
    fail("text recognition failed")
}

let observations = request.results ?? []
let blocks = observations.compactMap { observation -> Block? in
    guard let candidate = observation.topCandidates(1).first else {
        return nil
    }
    let box = observation.boundingBox
    return Block(
        text: candidate.string,
        confidence: candidate.confidence,
        x: box.origin.x,
        y: box.origin.y,
        width: box.size.width,
        height: box.size.height
    )
}.sorted { left, right in
    let leftRow = Int((left.y * 1000).rounded())
    let rightRow = Int((right.y * 1000).rounded())
    if leftRow != rightRow {
        return leftRow > rightRow
    }
    if left.x != right.x {
        return left.x < right.x
    }
    if left.y != right.y {
        return left.y > right.y
    }
    if left.text != right.text {
        return left.text < right.text
    }
    if left.width != right.width {
        return left.width < right.width
    }
    if left.height != right.height {
        return left.height < right.height
    }
    return left.confidence < right.confidence
}

do {
    let data = try JSONEncoder().encode(Output(blocks: blocks))
    FileHandle.standardOutput.write(data)
} catch {
    fail("OCR output could not be serialized")
}
