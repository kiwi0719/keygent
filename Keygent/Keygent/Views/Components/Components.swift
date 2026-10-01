import SwiftUI

// MARK: - Kbd

struct Kbd: View {
    enum Style { case light, active, onInk }

    let text: String
    var style: Style = .light
    var size: CGFloat = 11
    var weight: Font.Weight = .regular
    var minWidth: CGFloat? = nil

    init(_ text: String, style: Style = .light, size: CGFloat = 11, weight: Font.Weight = .regular, minWidth: CGFloat? = nil) {
        self.text = text
        self.style = style
        self.size = size
        self.weight = weight
        self.minWidth = minWidth
    }

    init(_ text: String, active: Bool, size: CGFloat = 11, weight: Font.Weight = .regular, minWidth: CGFloat? = nil) {
        self.init(text, style: active ? .active : .light, size: size, weight: weight, minWidth: minWidth)
    }

    var body: some View {
        Text(text)
            .font(KFont.mono(size, weight))
            .foregroundStyle(style == .light ? K.text2 : K.paper)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .frame(minWidth: minWidth)
            .background(RoundedRectangle(cornerRadius: 4).fill(fill))
            .overlay {
                if style != .onInk {
                    RoundedRectangle(cornerRadius: 4).strokeBorder(style == .active ? K.ink : K.border, lineWidth: 1)
                }
            }
    }

    private var fill: Color {
        switch style {
        case .light: return .white
        case .active: return K.ink
        case .onInk: return K.text2
        }
    }
}

// MARK: - Dot

struct Dot: View {
    var color: Color
    var size: CGFloat = 8
    var square = false
    var hollow = false

    var body: some View {
        RoundedRectangle(cornerRadius: square ? 2 : size / 2)
            .fill(hollow ? Color.white : color)
            .overlay {
                if hollow { RoundedRectangle(cornerRadius: square ? 2 : size / 2).strokeBorder(K.ink, lineWidth: 1) }
            }
            .frame(width: size, height: size)
    }
}

// MARK: - Labels

struct SectionLabel: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(KFont.mono(11))
            .tracking(1.1)
            .foregroundStyle(K.text3)
    }
}

struct HLine: View {
    var color: Color = K.line
    var body: some View { Rectangle().fill(color).frame(height: 1) }
}

struct VLine: View {
    var color: Color = K.line2
    var body: some View { Rectangle().fill(color).frame(width: 1) }
}

/// 启动器左侧那个黑底 `>_` 图标。`>` 像一只眼，隔几秒眯一下，`_` 跟着咧成笑。
struct PromptGlyph: View {
    var size: CGFloat = 34
    @State private var wink: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.265)
            .fill(K.ink)
            .frame(width: size, height: size)
            .overlay {
                PromptShape(wink: wink)
                    .stroke(K.paper, style: StrokeStyle(lineWidth: size > 30 ? 2.2 : 2.4, lineCap: .round, lineJoin: .round))
                    .frame(width: size * 0.53, height: size * 0.53)
            }
            .task(id: reduceMotion) {
                guard !reduceMotion else { wink = 0; return }
                try? await Task.sleep(for: .seconds(1.2))
                while !Task.isCancelled {
                    await blink()
                    // 偶尔连眨两下
                    if Double.random(in: 0..<1) < 0.25 {
                        try? await Task.sleep(for: .milliseconds(180))
                        await blink()
                    }
                    try? await Task.sleep(for: .seconds(Double.random(in: 2.8...5.5)))
                }
            }
    }

    private func blink() async {
        withAnimation(.easeIn(duration: 0.09)) { wink = 1 }
        try? await Task.sleep(for: .milliseconds(140))
        withAnimation(.spring(response: 0.28, dampingFraction: 0.55)) { wink = 0 }
        try? await Task.sleep(for: .milliseconds(260))
    }
}

/// `wink` 0 → 1：`>` 收成一道眯缝（还是 `>`，不压成横线，否则会读成 `-~`），`_` 弯成笑。
private struct PromptShape: Shape {
    var wink: CGFloat = 0

    var animatableData: CGFloat {
        get { wink }
        set { wink = newValue }
    }

    func path(in r: CGRect) -> Path {
        let s = r.width / 24
        let t = min(max(wink, 0), 1)
        // 眼：开口收到 28%，整只眼往右挪半格，眯起来更紧凑
        // 回弹时 wink 会略小于 0，眼睛稍微瞪大一点，挺可爱，但别太夸张
        let eyeH = 6 * min(1 - 0.72 * wink, 1.1)
        let dx = 0.5 * t
        var p = Path()
        p.move(to: CGPoint(x: (4 + dx) * s, y: (12 + eyeH) * s))
        p.addLine(to: CGPoint(x: (10 + dx) * s, y: 12 * s))
        p.addLine(to: CGPoint(x: (4 + dx) * s, y: (12 - eyeH) * s))
        // 嘴：两端不动，只把中间往下弯。两端也上翘的话，这么小的尺寸会抖成波浪线
        p.move(to: CGPoint(x: 12 * s, y: 18 * s))
        p.addQuadCurve(to: CGPoint(x: 20 * s, y: 18 * s),
                       control: CGPoint(x: 16 * s, y: (18 + 2.6 * t) * s))
        return p
    }
}

// MARK: - Bars

/// 面板最底部那条浅灰提示栏。
struct FooterBar<Leading: View, Trailing: View>: View {
    @ViewBuilder var leading: Leading
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack {
            leading
            Spacer(minLength: 12)
            trailing
        }
        .font(KFont.sans(12))
        .foregroundStyle(K.text2)
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity)
        .background(K.bar)
    }
}

struct EnterHint: View {
    let label: String
    var body: some View {
        HStack(spacing: 6) {
            Kbd("↵", size: 12, weight: .medium)
            Text(label)
        }
    }
}

struct KeyHint: Identifiable, Equatable {
    var id: String { k + t }
    let k: String
    let t: String
    init(_ k: String, _ t: String) { self.k = k; self.t = t }
}

/// 「模式 + 快捷键」状态栏，④ ⑤ 用。
struct KeyHintBar: View {
    let mode: String
    let keys: [KeyHint]
    var horizontal: CGFloat = 20

    var body: some View {
        FlowLayout(spacing: 16, lineSpacing: 6) {
            Text(mode).fontWeight(.bold)
            ForEach(keys) { k in
                HStack(spacing: 6) {
                    Kbd(k.k)
                    Text(k.t)
                }
            }
        }
        .font(KFont.sans(12))
        .foregroundStyle(K.text2)
        .padding(.horizontal, horizontal)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(K.bar)
    }
}

/// 窗口式列表（ListWindow）底下那行：眼前是第几到第几条、往上/往下挪一条（滚轮也行）。
struct WindowBar: View {
    let window: Range<Int>
    let total: Int
    var unit = "条"
    var up = "↑ 更近"
    var down = "↓ 更早"
    let scroll: (Int) -> Void

    var body: some View {
        HStack {
            Text("第 \(window.lowerBound + 1)–\(window.upperBound) \(unit) / 共 \(total) \(unit) · 编号随滚动重排")
                .font(KFont.sans(12))
                .foregroundStyle(K.text4)
                .lineLimit(1)
            Spacer()
            HStack(spacing: 6) {
                SmallButton(title: up, label: "向上滚动") { scroll(-1) }
                SmallButton(title: down, label: "向下滚动") { scroll(1) }
            }
        }
    }
}

// MARK: - Buttons

struct SmallButton: View {
    let title: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(KFont.sans(12))
                .foregroundStyle(K.text2)
                .padding(.horizontal, 10)
                .frame(height: 28)
                .background(RoundedRectangle(cornerRadius: 6).fill(.white))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(K.border))
                .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel(label)
    }
}

/// 返回按钮，样子和启动器的「+ 文件」一致。esc 一直标着；`showEsc` = 此刻按 Esc 是否就是点它，
/// Esc 被别的东西占用（输入中、过程展开）时只是变淡，不消失，宽度也不跳。
struct BackButton: View {
    let label: String
    var showEsc = true
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text("← \(label)")
                Kbd("esc").opacity(showEsc ? 1 : 0.4)
            }
            .font(KFont.sans(12))
            .foregroundStyle(K.text2)
            .padding(.leading, 10)
            .padding(.trailing, 8)
            .frame(height: 30)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(K.dash, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .fixedSize()
    }
}

struct InkButton: View {
    let title: String
    var kbd: String? = nil
    var height: CGFloat = 34
    var bold = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text(title).fontWeight(bold ? .bold : .regular)
                if let kbd { Kbd(kbd, style: .onInk) }
            }
            .font(KFont.sans(13))
            .foregroundStyle(K.paper)
            .padding(.horizontal, 12)
            .frame(height: height)
            .background(RoundedRectangle(cornerRadius: 8).fill(K.ink))
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .fixedSize()
    }
}

struct OutlineButton: View {
    let title: String
    var kbd: String? = nil
    var height: CGFloat = 34
    var stroke: Color = K.dash
    var kbdInline = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text(title)
                if let kbd { Kbd(kbd) }
            }
            .font(KFont.sans(13))
            .foregroundStyle(K.ink)
            .padding(.horizontal, 12)
            .frame(height: height)
            .background(RoundedRectangle(cornerRadius: 8).strokeBorder(stroke))
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .fixedSize()
    }
}

struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.7 : 1)
            .onHover { inside in
                if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
            }
    }
}

/// 列表行：hover 时给一点底色。
struct RowStyle: ButtonStyle {
    var selected: Bool
    var selectedFill: Color = .white
    var radius: CGFloat = 8
    var selectedStroke: Color? = nil

    func makeBody(configuration: Configuration) -> some View {
        HoverRow(configuration: configuration, selected: selected, selectedFill: selectedFill, radius: radius, selectedStroke: selectedStroke)
    }

    private struct HoverRow: View {
        let configuration: Configuration
        let selected: Bool
        let selectedFill: Color
        let radius: CGFloat
        let selectedStroke: Color?
        @State private var hover = false

        var body: some View {
            configuration.label
                .background(
                    RoundedRectangle(cornerRadius: radius)
                        .fill(selected ? selectedFill : (hover ? Color.black.opacity(0.035) : .clear))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: radius)
                        .strokeBorder(selected ? (selectedStroke ?? .clear) : .clear, lineWidth: 1)
                )
                .contentShape(Rectangle())
                .onHover { hover = $0 }
                .opacity(configuration.isPressed ? 0.8 : 1)
        }
    }
}

/// 对话气泡：你 = 墨底白字，Agent = 白底描边。
struct Bubble: View {
    enum Kind { case you, agent, step }
    let text: String
    let kind: Kind
    var maxWidthFraction: CGFloat = 0.8
    /// 只预览几行（任务页底下的「最近两句」），全文去过程里看
    var lineLimit: Int? = nil

    var body: some View {
        Text(text)
            .font(KFont.sans(13))
            .lineSpacing(3)
            .lineLimit(lineLimit)
            .truncationMode(.tail)
            .foregroundStyle(kind == .you ? K.paper : K.ink)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 10).fill(kind == .you ? K.ink : (kind == .agent ? .white : K.bar)))
            .overlay {
                if kind == .agent { RoundedRectangle(cornerRadius: 10).strokeBorder(K.line) }
            }
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Layout

struct FlowLayout: Layout {
    var spacing: CGFloat = 8
    var lineSpacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxW = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineH: CGFloat = 0, width: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0, x + sz.width > maxW {
                y += lineH + lineSpacing
                x = 0
                lineH = 0
            }
            x += sz.width + spacing
            lineH = max(lineH, sz.height)
            width = max(width, x - spacing)
        }
        return CGSize(width: proposal.width ?? width, height: y + lineH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var rows: [[(Subviews.Element, CGSize)]] = [[]]
        var x: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0, x + sz.width > bounds.width {
                rows.append([])
                x = 0
            }
            rows[rows.count - 1].append((s, sz))
            x += sz.width + spacing
        }
        var y = bounds.minY
        for row in rows {
            let h = row.map(\.1.height).max() ?? 0
            var px = bounds.minX
            for (s, sz) in row {
                s.place(at: CGPoint(x: px, y: y + (h - sz.height) / 2), proposal: ProposedViewSize(sz))
                px += sz.width + spacing
            }
            y += h + lineSpacing
        }
    }
}

// MARK: - Focus sync

extension View {
    /// 把 store 里的「请求聚焦」同步到 @FocusState，并把真实焦点回写给 store。
    func syncFocus(_ store: AppStore, _ binding: FocusState<InputField?>.Binding) -> some View {
        self
            .onChange(of: store.focusToken) { _, _ in
                binding.wrappedValue = store.focusRequest
            }
            .onChange(of: binding.wrappedValue) { _, v in
                store.focusedField = v
            }
            .onAppear {
                DispatchQueue.main.async { binding.wrappedValue = store.focusRequest }
            }
    }
}
