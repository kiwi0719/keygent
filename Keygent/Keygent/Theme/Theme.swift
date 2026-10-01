import SwiftUI
import AppKit

extension Color {
    init(hex: UInt32, alpha: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: alpha
        )
    }
}

/// Keygent 的调色板：暖灰纸面 + 墨黑，绿色 = 在跑/完成，琥珀 = 等你，红 = 出错。
enum K {
    static let ink = Color(hex: 0x121212)
    static let paper = Color(hex: 0xF6F5F1)
    static let bar = Color(hex: 0xEDEBE5)
    static let line = Color(hex: 0xDEDBD3)
    static let line2 = Color(hex: 0xECE9E2)
    static let line3 = Color(hex: 0xF1EFEA)
    static let border = Color(hex: 0xD2CEC5)
    static let dash = Color(hex: 0xB9B5AC)
    static let text2 = Color(hex: 0x3E3B36)
    static let text3 = Color(hex: 0x5E5B55)
    static let text4 = Color(hex: 0x8A867E)
    static let canvas = Color(hex: 0xCFCBC2)

    static let green = Color(hex: 0x0A6E4F)
    static let greenSoft = Color(hex: 0x3FB58A)
    static let greenBg = Color(hex: 0xE3F2EB)
    static let amber = Color(hex: 0xB7791F)
    static let amberBg = Color(hex: 0xFFF1C2)
    static let amberBg2 = Color(hex: 0xFFF6D6)
    static let red = Color(hex: 0xB3261E)
    static let redBg = Color(hex: 0xF9DEDC)
    static let chip = Color(hex: 0xE9E6DF)
    static let skeleton = Color(hex: 0xECE9E2)
}

enum KFont {
    static func sans(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight)
    }

    private static let hasJetBrains = NSFont(name: "JetBrains Mono", size: 12) != nil

    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        if hasJetBrains { return .custom("JetBrains Mono", size: size).weight(weight) }
        return .system(size: size, weight: weight, design: .monospaced)
    }
}
