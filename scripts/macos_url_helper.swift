import AppKit
import Carbon
import Foundation

let deniedSentinel = "__LARP_AUTOMATION_DENIED__"

struct BrowserSpec {
    let name: String
    let safariStyle: Bool
}

let browserSpecs = [
    BrowserSpec(name: "Safari", safariStyle: true),
    BrowserSpec(name: "Google Chrome", safariStyle: false),
    BrowserSpec(name: "Brave Browser", safariStyle: false),
    BrowserSpec(name: "Microsoft Edge", safariStyle: false),
    BrowserSpec(name: "Arc", safariStyle: false),
    BrowserSpec(name: "Comet", safariStyle: false),
    BrowserSpec(name: "Chromium", safariStyle: false),
    BrowserSpec(name: "Vivaldi", safariStyle: false),
    BrowserSpec(name: "Opera", safariStyle: false),
]

func writeResult(_ value: String, to path: String) {
    do {
        try value.write(toFile: path, atomically: true, encoding: .utf8)
    } catch {
        fputs("LARP URL Helper could not write its result.\n", stderr)
    }
}

func browserIsRunning(_ name: String) -> Bool {
    NSWorkspace.shared.runningApplications.contains {
        $0.localizedName == name && !$0.isTerminated
    }
}

func runningBrowserApplication(_ name: String) -> NSRunningApplication? {
    NSWorkspace.shared.runningApplications.first {
        $0.localizedName == name && !$0.isTerminated
    }
}

func requestAutomationPermission(for application: NSRunningApplication) -> OSStatus {
    guard let bundleIdentifier = application.bundleIdentifier else {
        return OSStatus(paramErr)
    }
    let target = NSAppleEventDescriptor(bundleIdentifier: bundleIdentifier)
    return AEDeterminePermissionToAutomateTarget(
        target.aeDesc,
        AEEventClass(typeWildCard),
        AEEventID(typeWildCard),
        true
    )
}

func activeURLScript(for browser: BrowserSpec) -> String {
    if browser.safariStyle {
        return """
        tell application "\(browser.name)"
          if (count of windows) is 0 then return ""
          return URL of current tab of front window
        end tell
        """
    }
    return """
    tell application "\(browser.name)"
      if (count of windows) is 0 then return ""
      return URL of active tab of front window
    end tell
    """
}

func isAutomationDenial(_ errorInfo: [String: Any]) -> Bool {
    let number = (errorInfo["NSAppleScriptErrorNumber"] as? NSNumber)?.intValue
    if number == -1743 {
        return true
    }
    let message = String(describing: errorInfo["NSAppleScriptErrorMessage"] ?? "").lowercased()
    return message.contains("not authorized") || message.contains("not permitted")
}

guard CommandLine.arguments.count >= 2 else {
    exit(2)
}

let outputPath = CommandLine.arguments[1]
let runningBrowsers = browserSpecs.filter { browserIsRunning($0.name) }
var fallbackURL = ""
var automationDenied = false

for browser in runningBrowsers {
    guard let application = runningBrowserApplication(browser.name) else {
        continue
    }
    let permissionStatus = requestAutomationPermission(for: application)
    if permissionStatus == errAEEventNotPermitted {
        automationDenied = true
        continue
    }
    if permissionStatus != noErr {
        continue
    }

    guard let script = NSAppleScript(source: activeURLScript(for: browser)) else {
        continue
    }

    var errorInfo: NSDictionary?
    let result = script.executeAndReturnError(&errorInfo)

    if let errorInfo = errorInfo as? [String: Any] {
        if isAutomationDenial(errorInfo) {
            automationDenied = true
        }
        continue
    }

    let url = (result.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if url.isEmpty {
        continue
    }
    if url.range(of: #"linkedin\.com/in/"#, options: .regularExpression) != nil {
        writeResult(url, to: outputPath)
        exit(0)
    }
    if fallbackURL.isEmpty {
        fallbackURL = url
    }
}

if !fallbackURL.isEmpty {
    writeResult(fallbackURL, to: outputPath)
} else if automationDenied {
    writeResult(deniedSentinel, to: outputPath)
} else {
    writeResult("", to: outputPath)
}
