import SwiftUI
import AppKit

enum CatapultIcon {
    static let appLogo = "arrow.up.forward.app"
    static let activity = "list.bullet.rectangle"
    static let account = "person.crop.circle"
    static let appID = "app"
    static let extensionAppID = "puzzlepiece.extension"
    static let chooseFile = "folder"
    static let ipaFile = "doc.badge.plus"
    static let install = "square.and.arrow.down"
    static let refresh = "arrow.clockwise"
    static let copy = "doc.on.doc"
    static let delete = "trash"
    static let signOut = "rectangle.portrait.and.arrow.right"
    static let ready = "checkmark.circle"
    static let warning = "exclamationmark.triangle"
    static let stopped = "pause.circle"
    static let setup = "cable.connector"
    static let noDevices = "wifi.slash"
    static let emptyAppIDs = "app.dashed"
    static let unknown = "questionmark.circle"

    static func device(for device: Device?) -> String {
        guard let device else {
            return unknown
        }

        switch device.deviceClass {
        case "ios":
            return "iphone"
        case "ipados":
            return "ipad"
        case "tvos":
            return "appletv"
        case "macos":
            return "desktopcomputer"
        case "homepod":
            return "homepod"
        default:
            return unknown
        }
    }

    static func activityKind(_ kind: String) -> String {
        switch kind.lowercased() {
        case "install":
            return install
        case "sign", "resign":
            return "signature"
        case "device_setup", "setup", "pair":
            return setup
        case "account", "provision":
            return account
        default:
            return activity
        }
    }

    static func status(_ status: String?) -> String {
        switch status?.lowercased() {
        case "done", "complete", "completed", "success", "succeeded":
            return ready
        case "failed", "failure", "error":
            return warning
        case "cancelled", "canceled":
            return stopped
        default:
            return activity
        }
    }
}

enum CatapultAsset {
    static let appIcon = loadImage(named: "CatapultIcon", extension: "png")
    static let menuBarTemplate: NSImage? = {
        guard let image = loadImage(named: "CatapultMenuBarTemplate", extension: "png") else {
            return nil
        }
        image.isTemplate = true
        return image
    }()

    private static func loadImage(named name: String, extension ext: String) -> NSImage? {
        guard let url = Bundle.main.url(forResource: name, withExtension: ext) else {
            return nil
        }
        return NSImage(contentsOf: url)
    }
}

struct CatapultBrandIcon: View {
    var size: CGFloat = 28

    var body: some View {
        if let image = CatapultAsset.appIcon {
            Image(nsImage: image)
                .resizable()
                .interpolation(.high)
                .frame(width: size, height: size)
                .clipShape(RoundedRectangle(cornerRadius: size * 0.23))
        } else {
            Image(systemName: CatapultIcon.appLogo)
                .font(.system(size: size * 0.56, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: size, height: size)
                .background(Color.accentColor, in: RoundedRectangle(cornerRadius: size * 0.25))
        }
    }
}

struct CatapultMenuBarIcon: View {
    var size: CGFloat = 12

    var body: some View {
        Image(systemName: "arrow.up.forward")
            .font(.system(size: size, weight: .semibold))
    }
}

struct CatapultIconTile: View {
    let systemName: String
    var tint: Color = .accentColor
    var dimension: CGFloat = 34
    var font: Font = .title3

    var body: some View {
        Image(systemName: systemName)
            .font(font)
            .foregroundStyle(tint)
            .frame(width: dimension, height: dimension)
            .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct CatapultStatusPill: View {
    let title: String
    let color: Color
    var showsDot = true

    var body: some View {
        HStack(spacing: 5) {
            if showsDot {
                Circle()
                    .fill(color)
                    .frame(width: 5, height: 5)
            }
            Text(title)
                .lineLimit(1)
        }
        .font(.caption2.weight(.semibold))
        .foregroundStyle(color)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(color.opacity(0.12), in: Capsule())
    }
}
