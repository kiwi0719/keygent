import SwiftUI
import AppKit

/// 多行等宽编辑器（设置页的 JSON / SKILL.md 共用）。包一层 NSTextView：关掉智能引号、自动替换、拼写检查——
/// 不然在 JSON 里打 " 会变成弯引号。焦点跟着 store 的“请求聚焦”走（focus 等于请求的那个时成为第一响应者）。
struct CodeEditor: NSViewRepresentable {
    @Binding var text: String
    var editable = true
    var focus: InputField = .settingsEditor
    @Environment(AppStore.self) private var store

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        let tv = scroll.documentView as! NSTextView
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.allowsUndo = true
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false
        tv.isAutomaticSpellingCorrectionEnabled = false
        tv.isContinuousSpellCheckingEnabled = false
        tv.isGrammarCheckingEnabled = false
        tv.smartInsertDeleteEnabled = false
        tv.font = NSFont(name: "JetBrains Mono", size: 13) ?? .monospacedSystemFont(ofSize: 13, weight: .regular)
        tv.textColor = NSColor(K.ink)
        tv.insertionPointColor = NSColor(K.ink)
        tv.drawsBackground = false
        tv.textContainerInset = NSSize(width: 0, height: 6)
        tv.string = text
        tv.isEditable = editable
        context.coordinator.textView = tv
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = context.coordinator.textView else { return }
        context.coordinator.parent = self
        if tv.string != text { tv.string = text }
        tv.isEditable = editable
        // 请求聚焦到这里：等 SwiftUI 处理完它那边的焦点再抢（不然会被它清掉）
        if store.focusRequest == focus, context.coordinator.lastToken != store.focusToken {
            context.coordinator.lastToken = store.focusToken
            DispatchQueue.main.async {
                guard let w = tv.window else { return }
                w.makeFirstResponder(tv)
                store.focusedField = focus
            }
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: CodeEditor
        weak var textView: NSTextView?
        var lastToken = -1

        init(_ parent: CodeEditor) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            parent.text = tv.string
        }

        func textDidBeginEditing(_ notification: Notification) {
            parent.store.focusedField = parent.focus
        }
    }
}
