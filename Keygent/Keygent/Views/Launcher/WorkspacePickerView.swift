import SwiftUI

/// ⌘E 弹出的「在哪个文件夹里干活」：⌘0 临时、⌘数字 = 最近用过的里眼前第几个、⌘O 从访达选。
struct WorkspacePickerView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        let recent = store.recentWorkspaces
        let win = store.wsWindow
        let L = store.launcher

        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text("工作区")
                    .font(KFont.sans(15, .bold))
                Spacer()
                Text("新任务在这个文件夹里读写")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text4)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 14)

            HLine(color: K.line2)

            VStack(spacing: 2) {
                row(icon: "~", title: "临时", sub: "每个任务一个空目录，只用全局记忆",
                    key: "⌘0", current: L.workdir == nil, highlighted: L.wsPick == 0) {
                    store.setWorkspace(nil)
                }

                HStack {
                    SectionLabel("最近用过的")
                    Spacer()
                }
                .padding(.horizontal, 8)
                .padding(.top, 8)
                .padding(.bottom, 2)

                if recent.isEmpty {
                    Button { store.openFolder() } label: {
                        HStack(spacing: 6) {
                            Text("还没有用过的文件夹 ·")
                            Kbd("⌘O")
                            Text("从访达选")
                        }
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text4)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 18)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                }

                ForEach(win, id: \.self) { j in
                    let p = recent[j]
                    row(icon: "DIR", title: Workspaces.name(p), sub: Workspaces.short(p),
                        key: "⌘\(j - win.lowerBound + 1)", current: L.workdir == p, highlighted: L.wsPick == j + 1) {
                        store.setWorkspace(p)
                    }
                }

                if recent.count > ListWindow.size {
                    WindowBar(window: win, total: recent.count, unit: "个") { store.wsScroll($0) }
                        .padding(.horizontal, 8)
                        .padding(.top, 6)
                }
            }
            .padding(10)

            HStack {
                HStack(spacing: 5) { Kbd("⌘O"); Text("访达") }
                Spacer()
                Button {
                    store.launcher.wsPicker = false
                    store.requestFocus(.launcher)
                } label: {
                    HStack(spacing: 6) {
                        Kbd("esc")
                        Text("关闭")
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableStyle())
            }
            .font(KFont.sans(12))
            .foregroundStyle(K.text2)
            .padding(.horizontal, 18)
            .padding(.vertical, 10)
            .background(K.paper)
        }
        .frame(width: 580)
        .background(Color.white)
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .shadow(color: .black.opacity(0.28), radius: 24, y: 12)
    }

    private func row(icon: String, title: String, sub: String, key: String,
                     current: Bool, highlighted: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Text(icon)
                    .font(KFont.mono(9))
                    .foregroundStyle(K.text3)
                    .frame(width: 30, height: 30)
                    .background(RoundedRectangle(cornerRadius: 4).fill(K.line3))
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(KFont.sans(14)).lineLimit(1)
                    Text(sub).font(KFont.sans(12)).foregroundStyle(K.text4).lineLimit(1).truncationMode(.middle)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if current {
                    Text("当前")
                        .font(KFont.sans(12))
                        .foregroundStyle(K.green)
                }
                Kbd(key, active: highlighted)
            }
            .foregroundStyle(K.ink)
            .padding(8)
        }
        .buttonStyle(RowStyle(selected: highlighted, selectedFill: K.paper, radius: 6))
    }
}
