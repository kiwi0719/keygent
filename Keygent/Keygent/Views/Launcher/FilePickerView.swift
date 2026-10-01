import SwiftUI

/// ⌘O 弹出的「最近用过的文件」（在 Keygent 里附加过的）。窗口式列表：⌘数字 = 眼前第几个，选中/取消。
struct FilePickerView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        let recent = store.recentFiles
        let win = store.fileWindow
        let L = store.launcher

        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text("添加文件")
                    .font(KFont.sans(15, .bold))
                Spacer()
                Text("也可以直接拖进来")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text4)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 14)

            HLine(color: K.line2)

            HStack {
                SectionLabel("最近用过的文件")
                Spacer()
                if !recent.isEmpty {
                    Text("⌘ + 数字 = 眼前第几个")
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text4)
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 10)
            .padding(.bottom, 4)

            if recent.isEmpty {
                Button { store.openFinder() } label: {
                    VStack(spacing: 6) {
                        Text("还没有用过的文件")
                            .font(KFont.sans(13))
                            .foregroundStyle(K.text3)
                        HStack(spacing: 6) {
                            Kbd("⌘⇧O")
                            Text("从访达选")
                        }
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text4)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 26)
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableStyle())
            } else {
                VStack(spacing: 2) {
                    ForEach(win, id: \.self) { i in
                        let f = recent[i]
                        let on = store.isAttached(f)
                        Button {
                            store.launcher.filePick = i
                            store.toggleFile(f)
                        } label: {
                            HStack(spacing: 12) {
                                Text(RecentFiles.ext(f))
                                    .font(KFont.mono(9))
                                    .foregroundStyle(K.text3)
                                    .frame(width: 30, height: 30)
                                    .background(RoundedRectangle(cornerRadius: 4).fill(K.line3))
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(f.lastPathComponent).font(KFont.sans(14)).lineLimit(1)
                                    Text(RecentFiles.whereLabel(f)).font(KFont.sans(12)).foregroundStyle(K.text4).lineLimit(1)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                if on {
                                    Text("已添加")
                                        .font(KFont.sans(12))
                                        .foregroundStyle(K.green)
                                }
                                Kbd("⌘\(i - win.lowerBound + 1)", active: on)
                            }
                            .foregroundStyle(K.ink)
                            .padding(8)
                        }
                        .buttonStyle(RowStyle(selected: L.filePick == i, selectedFill: K.paper, radius: 6))
                    }

                    if recent.count > ListWindow.size {
                        WindowBar(window: win, total: recent.count, unit: "个") { store.fileScroll($0) }
                            .padding(.horizontal, 8)
                            .padding(.top, 6)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.bottom, 10)
            }

            HStack {
                Text("↑↓ 空格 选中/取消 · ⌘数字 直接选 · ⌘⇧O 打开系统访达")
                Spacer()
                Button {
                    store.launcher.picker = false
                    store.requestFocus(.launcher)
                } label: {
                    HStack(spacing: 6) {
                        Kbd("↵")
                        Text("完成")
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
}
