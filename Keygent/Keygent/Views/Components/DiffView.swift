import SwiftUI

/// 统一 diff 的一行
struct DiffLine: Identifiable, Equatable {
    enum Kind { case hunk, context, added, removed, note }
    let id: Int
    let kind: Kind
    let text: String
    let old: Int?
    let new: Int?

    /// weaverd 给的统一 diff（没有 ---/+++ 文件头）拆成行，算好两列行号
    static func parse(_ diff: String) -> [DiffLine] {
        var out: [DiffLine] = []
        var o = 0, n = 0
        for raw in diff.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            if line.isEmpty { continue }      // 末尾 split 多出来的；真正的空行带着前缀空格
            let i = out.count
            if line.hasPrefix("@@") {
                // @@ -12,3 +12,4 @@
                let parts = line.split(separator: " ")
                if parts.count >= 3 {
                    o = Int(parts[1].dropFirst().split(separator: ",").first ?? "") ?? 0
                    n = Int(parts[2].dropFirst().split(separator: ",").first ?? "") ?? 0
                }
                out.append(DiffLine(id: i, kind: .hunk, text: line, old: nil, new: nil))
            } else if line.hasPrefix("+") {
                out.append(DiffLine(id: i, kind: .added, text: String(line.dropFirst()), old: nil, new: n))
                n += 1
            } else if line.hasPrefix("-") {
                out.append(DiffLine(id: i, kind: .removed, text: String(line.dropFirst()), old: o, new: nil))
                o += 1
            } else if line.hasPrefix(" ") {
                out.append(DiffLine(id: i, kind: .context, text: String(line.dropFirst()), old: o, new: n))
                o += 1
                n += 1
            } else if line.hasPrefix("\\") {
                continue                     // \ No newline at end of file
            } else if !line.isEmpty {
                out.append(DiffLine(id: i, kind: .note, text: line, old: nil, new: nil))
            }
        }
        return out
    }

    static func counts(_ lines: [DiffLine]) -> (added: Int, removed: Int) {
        (lines.filter { $0.kind == .added }.count, lines.filter { $0.kind == .removed }.count)
    }
}

/// 改了几行：`+9 -1`，加的绿、删的红（视频里结果卡底部那一行）
struct ChangeCount: View {
    let added: Int
    let removed: Int
    var size: CGFloat = 12

    var body: some View {
        HStack(spacing: 6) {
            if added > 0 || removed == 0 { Text("+\(added)").foregroundStyle(K.green) }
            if removed > 0 { Text("-\(removed)").foregroundStyle(K.red) }
        }
        .font(KFont.mono(size))
        .fixedSize()
    }
}

/// diff 视图：等宽字，两列行号，加的行浅绿底、删的行浅红底，段落头灰字。
/// maxLines：最多显示几行（多的折起来，下面一行写着还有几行；nil = 全部）。
struct DiffView: View {
    let lines: [DiffLine]
    var maxLines: Int? = nil
    var size: CGFloat = 12

    init(_ diff: String, maxLines: Int? = nil, size: CGFloat = 12) {
        lines = DiffLine.parse(diff)
        self.maxLines = maxLines
        self.size = size
    }

    var body: some View {
        let shown = maxLines.map { Array(lines.prefix($0)) } ?? lines
        let hidden = lines.count - shown.count
        let width = CGFloat(max(2, String(lines.compactMap { $0.new ?? $0.old }.max() ?? 0).count)) * size * 0.62 + 6

        VStack(alignment: .leading, spacing: 0) {
            ForEach(shown) { l in
                row(l, width: width)
            }
            if hidden > 0 {
                Text("还有 \(hidden) 行")
                    .font(KFont.sans(11))
                    .foregroundStyle(K.text4)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func row(_ l: DiffLine, width: CGFloat) -> some View {
        switch l.kind {
        case .hunk, .note:
            Text(l.kind == .hunk ? Self.hunkLabel(l.text) : l.text)
                .font(KFont.mono(size - 1))
                .foregroundStyle(K.text4)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(K.line3)
        default:
            HStack(alignment: .top, spacing: 0) {
                Text(l.old.map(String.init) ?? "")
                    .frame(width: width, alignment: .trailing)
                Text(l.new.map(String.init) ?? "")
                    .frame(width: width, alignment: .trailing)
                Text(l.kind == .added ? "+" : l.kind == .removed ? "-" : " ")
                    .frame(width: 16)
                    .foregroundStyle(l.kind == .added ? K.green : l.kind == .removed ? K.red : K.text4)
                Text(l.text.isEmpty ? " " : l.text)
                    .foregroundStyle(K.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .font(KFont.mono(size))
            .foregroundStyle(K.text4)
            .padding(.vertical, 1)
            .padding(.trailing, 8)
            .background(l.kind == .added ? K.greenBg : l.kind == .removed ? K.redBg.opacity(0.7) : .clear)
        }
    }

    /// `@@ -12,3 +12,4 @@ def foo():` → `第 12 行 · def foo():`
    static func hunkLabel(_ s: String) -> String {
        let parts = s.split(separator: " ", maxSplits: 4)
        let start = parts.count > 2 ? (parts[2].dropFirst().split(separator: ",").first.map(String.init) ?? "") : ""
        let tail = s.components(separatedBy: "@@").last?.trimmingCharacters(in: .whitespaces) ?? ""
        return (start.isEmpty || start == "0" ? "新文件" : "第 \(start) 行") + (tail.isEmpty ? "" : " · \(tail)")
    }
}

/// 一张装 diff 的白卡：表头一行（文件名 + 改了几行），下面 diff，太高就在卡里滚动。
struct DiffCard: View {
    let title: String
    let diff: String
    var maxHeight: CGFloat = 260
    var trailing: AnyView? = nil

    var body: some View {
        let lines = DiffLine.parse(diff)
        let c = DiffLine.counts(lines)
        VStack(spacing: 0) {
            if !title.isEmpty {         // 没标题（审批卡里，路径已经在卡片标题上）就不要表头
                HStack(spacing: 10) {
                    Text(title)
                        .font(KFont.mono(12))
                        .foregroundStyle(K.text2)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    ChangeCount(added: c.added, removed: c.removed, size: 11)
                    Spacer()
                    if let trailing { trailing }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .overlay(alignment: .bottom) { HLine(color: K.line2) }
            }
            ScrollView {
                if lines.isEmpty {
                    Text("没有可显示的改动")
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text4)
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    DiffView(diff).padding(.vertical, 4)
                }
            }
            .frame(maxHeight: maxHeight)
            .fixedSize(horizontal: false, vertical: true)
        }
        .background(RoundedRectangle(cornerRadius: 10).fill(.white))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(K.line))
    }
}
