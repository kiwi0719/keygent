import SwiftUI
import AppKit
import MarkdownUI

/// 任务结论是 Markdown（api.md 2.4 `final`）。解析和排版交给 MarkdownUI（cmark-gfm：表格、任务列表、嵌套列表、删除线都支持），
/// 这里只定主题，让它跟 Keygent 的纸面 + 墨黑对上。
struct MarkdownView: View {
    let text: String
    var size: CGFloat = 13
    var spacing: CGFloat = 8
    /// 图片的相对路径按这个目录算（任务的工作目录）
    var baseDir: String? = nil

    var body: some View {
        Markdown(MarkdownCJK.normalize(text), imageBaseURL: baseDir.map { URL(fileURLWithPath: $0, isDirectory: true) })
            .markdownTheme(.keygent(size: size, spacing: spacing))
            .markdownImageProvider(KeygentImageProvider())
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
    }
}

/// 结论里的 `![说明](地址)`：本机文件直接读，http(s) 异步下载；按卡片宽度缩放，最高 320，点一下用预览打开。
struct KeygentImageProvider: ImageProvider {
    func makeImage(url: URL?) -> some View {
        MarkdownImage(url: url)
    }
}

private struct MarkdownImage: View {
    let url: URL?

    var body: some View {
        Group {
            if let url, url.isFileURL {
                if let img = NSImage(contentsOf: url) {
                    framed(Image(nsImage: img).resizable(), size: img.size)
                } else {
                    failed
                }
            } else if let url, ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let image): framed(image.resizable(), size: nil)
                    case .failure: failed
                    default:
                        RoundedRectangle(cornerRadius: 6).fill(K.skeleton).frame(width: 160, height: 90)
                    }
                }
            } else {
                failed
            }
        }
        .markdownMargin(top: 0, bottom: 8)
    }

    private func framed(_ image: Image, size: CGSize?) -> some View {
        // 知道原图大小就算出贴合的尺寸，边框才能贴着图；不知道（网络图）就按上限等比缩放
        let fit: CGSize? = size.flatMap { s in
            guard s.width > 0, s.height > 0 else { return nil }
            var w = min(s.width, 640), h = w * s.height / s.width
            if h > 320 { h = 320; w = h * s.width / s.height }
            return CGSize(width: w, height: h)
        }
        return image
            .aspectRatio(contentMode: .fit)
            .frame(width: fit?.width, height: fit?.height)
            .frame(maxWidth: 640, maxHeight: 320, alignment: .leading)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(K.line2))
            .onTapGesture { if let url { NSWorkspace.shared.open(url) } }
            .onHover { if $0 { NSCursor.pointingHand.push() } else { NSCursor.pop() } }
            .help(url?.isFileURL == true ? url!.path : url?.absoluteString ?? "")
    }

    private var failed: some View {
        HStack(spacing: 6) {
            Image(systemName: "photo").font(.system(size: 11))
            Text("[图片：\(url?.lastPathComponent ?? "打不开")]")
        }
        .font(KFont.sans(12))
        .foregroundStyle(K.text4)
    }
}

/// cmark-gfm 按规范解析，中文里有两处会翻车，交给 MarkdownUI 之前先修掉：
/// 1. `**结论：…。**墨尔本`：收尾的 `**` 前面是标点、后面紧跟汉字，不算 right-flanking，粗体不闭合，`**` 原样露出来。
///    在 `*` 串和标点之间塞一个零宽空格（U+200B 既不算空白也不算标点），让它两头都能匹配。
/// 2. `12~13°C … 70~88%`：GFM 删除线认单个 `~`，范围号会把中间整段划掉。只保留 `~~` 删除线，单个 `~` 转义掉。
/// 代码块、行内代码、autolink 原样跳过。
enum MarkdownCJK {
    private static let zwsp: Unicode.Scalar = "\u{200B}"

    static func normalize(_ text: String) -> String {
        guard text.contains("*") || text.contains("~") else { return text }
        var fence: (char: Character, count: Int)?
        return text.split(separator: "\n", omittingEmptySubsequences: false).map { line in
            if let f = fenceMarker(line) {
                if let open = fence {
                    if f.char == open.char && f.count >= open.count && f.closable { fence = nil }
                } else {
                    fence = (f.char, f.count)
                }
                return String(line)
            }
            return fence != nil ? String(line) : inline(Array(line.unicodeScalars))
        }.joined(separator: "\n")
    }

    /// ``` / ~~~ 围栏行（最多 3 个空格缩进）。closable：围栏后面没有 info string 才能收尾。
    private static func fenceMarker(_ line: Substring) -> (char: Character, count: Int, closable: Bool)? {
        let indent = line.prefix(while: { $0 == " " })
        guard indent.count <= 3 else { return nil }
        let rest = line.dropFirst(indent.count)
        guard let c = rest.first, c == "`" || c == "~" else { return nil }
        let run = rest.prefix(while: { $0 == c }).count
        guard run >= 3 else { return nil }
        let info = rest.dropFirst(run)
        if c == "`" && info.contains("`") { return nil }
        return (c, run, info.allSatisfy { $0 == " " || $0 == "\t" })
    }

    private static func inline(_ s: [Unicode.Scalar]) -> String {
        var out = String.UnicodeScalarView()
        var i = 0
        while i < s.count {
            let c = s[i]
            switch c {
            case "\\":
                out.append(c)
                if i + 1 < s.count { out.append(s[i + 1]) }
                i += 2
            case "`":
                let n = runLength(s, i)
                if let end = closingBackticks(s, from: i + n, count: n) {
                    out.append(contentsOf: s[i..<end])
                    i = end
                } else {
                    out.append(contentsOf: s[i..<(i + n)])
                    i += n
                }
            case "<":
                if let end = autolinkEnd(s, i) {
                    out.append(contentsOf: s[i..<end])
                    i = end
                } else {
                    out.append(c)
                    i += 1
                }
            case "*":
                let n = runLength(s, i)
                let prev = i > 0 ? s[i - 1] : nil
                let next = i + n < s.count ? s[i + n] : nil
                if isPunct(prev) && isWordChar(next) { out.append(zwsp) }
                out.append(contentsOf: s[i..<(i + n)])
                if isPunct(next) && isWordChar(prev) { out.append(zwsp) }
                i += n
            case "~":
                let n = runLength(s, i)
                if n == 1 && !inURL(s, i) { out.append("\\") }
                out.append(contentsOf: s[i..<(i + n)])
                i += n
            default:
                out.append(c)
                i += 1
            }
        }
        return String(out)
    }

    private static func runLength(_ s: [Unicode.Scalar], _ i: Int) -> Int {
        var j = i
        while j < s.count && s[j] == s[i] { j += 1 }
        return j - i
    }

    /// 行内代码的收尾反引号串（长度必须相等），返回它之后的位置。
    private static func closingBackticks(_ s: [Unicode.Scalar], from: Int, count: Int) -> Int? {
        var j = from
        while j < s.count {
            if s[j] == "`" {
                let n = runLength(s, j)
                if n == count { return j + n }
                j += n
            } else {
                j += 1
            }
        }
        return nil
    }

    /// `<https://…>` / `<a@b.c>`：中间没有空白、有 `:` 或 `@`。
    private static func autolinkEnd(_ s: [Unicode.Scalar], _ i: Int) -> Int? {
        var j = i + 1
        var marked = false
        while j < s.count {
            let c = s[j]
            if c == ">" { return marked ? j + 1 : nil }
            if c == "<" || isSpace(c) { return nil }
            if c == ":" || c == "@" { marked = true }
            j += 1
        }
        return nil
    }

    /// `~` 所在的词（到前一个空白为止）像 URL 就不碰，`https://x.com/~user` 这种。
    private static func inURL(_ s: [Unicode.Scalar], _ i: Int) -> Bool {
        var j = i
        while j > 0 && !isSpace(s[j - 1]) { j -= 1 }
        let word = String(String.UnicodeScalarView(s[j..<i]))
        return word.contains("://") || word.contains("www.") || word.hasSuffix("/")
    }

    // 跟 cmark 的 flanking 判定一致：空白 = Zs + 制表/换行，标点 = ASCII 标点 + Unicode P 类。
    private static func isSpace(_ c: Unicode.Scalar) -> Bool {
        c.properties.generalCategory == .spaceSeparator || c == "\t" || c == "\n" || c == "\r" || c == "\u{0C}"
    }

    private static func isPunct(_ c: Unicode.Scalar?) -> Bool {
        guard let c else { return false }
        if c.isASCII { return CharacterSet.punctuationCharacters.contains(c) || CharacterSet.symbols.contains(c) }
        return CharacterSet.punctuationCharacters.contains(c)
    }

    private static func isWordChar(_ c: Unicode.Scalar?) -> Bool {
        guard let c else { return false }
        return !isSpace(c) && !isPunct(c)
    }
}

extension Theme {
    private static let monoFamily: FontProperties.Family =
        NSFont(name: "JetBrains Mono", size: 12) != nil ? .custom("JetBrains Mono") : .system(.monospaced)

    static func keygent(size: CGFloat, spacing: CGFloat) -> Theme {
        Theme()
            .text {
                FontSize(size)
                ForegroundColor(K.ink)
            }
            .code {
                FontFamily(monoFamily)
                FontSize(.em(0.88))
                BackgroundColor(K.line3)
            }
            .strong { FontWeight(.semibold) }
            .link {
                ForegroundColor(K.green)
                UnderlineStyle(.single)
            }
            .heading1 { c in heading(c, scale: (size + 9) / size, weight: .black, top: 4, bottom: spacing) }
            .heading2 { c in heading(c, scale: (size + 5) / size, weight: .black, top: 4, bottom: spacing) }
            .heading3 { c in heading(c, scale: (size + 2) / size, weight: .bold, top: 2, bottom: spacing) }
            .heading4 { c in heading(c, scale: 1, weight: .bold, top: 2, bottom: spacing) }
            .heading5 { c in heading(c, scale: 1, weight: .semibold, top: 2, bottom: spacing) }
            .heading6 { c in heading(c, scale: 1, weight: .semibold, top: 2, bottom: spacing, color: K.text3) }
            .paragraph { c in
                c.label
                    .fixedSize(horizontal: false, vertical: true)
                    .lineSpacing(size * 0.45)
                    .markdownMargin(top: 0, bottom: spacing)
            }
            .listItem { c in
                c.label.markdownMargin(top: .em(0.25))
            }
            .bulletedListMarker { _ in
                Text("•")
                    .foregroundStyle(K.text3)
                    .relativeFrame(minWidth: .em(1.2), alignment: .trailing)
            }
            .numberedListMarker { c in
                Text("\(c.itemNumber).")
                    .font(KFont.mono(size - 1))
                    .foregroundStyle(K.text3)
                    .relativeFrame(minWidth: .em(1.5), alignment: .trailing)
            }
            .taskListMarker { c in
                Image(systemName: c.isCompleted ? "checkmark.square.fill" : "square")
                    .foregroundStyle(c.isCompleted ? K.green : K.text4)
                    .relativeFrame(minWidth: .em(1.5), alignment: .trailing)
            }
            .blockquote { c in
                c.label
                    .markdownTextStyle { ForegroundColor(K.text2) }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 6).fill(K.paper))
                    .markdownMargin(top: 0, bottom: spacing)
            }
            .codeBlock { c in
                ScrollView(.horizontal, showsIndicators: false) {
                    c.label
                        .fixedSize(horizontal: false, vertical: true)
                        .lineSpacing(3)
                        .markdownTextStyle {
                            FontFamily(monoFamily)
                            FontSize(.em(0.92))
                            BackgroundColor(nil)
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 6).fill(K.line3))
                .markdownMargin(top: 0, bottom: spacing)
            }
            .table { c in
                c.label
                    .fixedSize(horizontal: false, vertical: true)
                    .markdownTableBorderStyle(.init(.horizontalBorders, color: K.line2))
                    .markdownTableBackgroundStyle(.clear)
                    .markdownMargin(top: 0, bottom: spacing)
            }
            .tableCell { c in
                c.label
                    .markdownTextStyle {
                        if c.row == 0 {
                            FontSize(.em(0.93))
                            ForegroundColor(K.text3)
                        } else if c.column > 0 {
                            ForegroundColor(K.text2)
                        }
                        BackgroundColor(nil)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 7)
                    .padding(.trailing, 18)
            }
            .thematicBreak {
                Rectangle()
                    .fill(K.line2)
                    .frame(height: 1)
                    .markdownMargin(top: spacing, bottom: spacing)
            }
    }

    private static func heading(
        _ c: BlockConfiguration, scale: CGFloat, weight: Font.Weight, top: CGFloat, bottom: CGFloat, color: Color = K.ink
    ) -> some View {
        c.label
            .markdownTextStyle {
                FontSize(.em(scale))
                FontWeight(weight)
                ForegroundColor(color)
            }
            .markdownMargin(top: top, bottom: bottom)
    }
}
