import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import "SoundCloudModel.js" as SoundCloudModel

BarWidget {
  id: root
  moduleName: "brunosilveira.soundcloud"

  property bool popupOpen: false
  property bool running: false
  property bool loggedIn: false
  property bool playing: false
  property string title: ""
  property string artist: ""
  property string artUrl: ""
  property real duration: 0
  property real position: 0
  property string lastError: ""
  property bool actionBusy: false

  readonly property string helperPath: {
    var value = Qt.resolvedUrl("soundcloud_app.py").toString()
    return value.indexOf("file://") === 0 ? decodeURIComponent(value.substring(7)) : value
  }
  readonly property string label: SoundCloudModel.displayLabel({ trackTitle: title, trackArtist: artist })
  readonly property string playIcon: playing ? "󰏤" : "󰐊"
  readonly property color dim: Qt.darker(bar.barForeground, 1.5)

  function close() { popupOpen = false }

  function refresh() {
    if (statusProcess.running) return
    statusProcess.command = ["python3", helperPath, "status"]
    statusProcess.running = true
  }

  function runAction(action) {
    if (actionProcess.running) return
    actionBusy = true
    lastError = ""
    actionProcess.command = ["python3", helperPath, action]
    actionProcess.running = true
  }

  function applyStatus(raw) {
    try {
      var state = JSON.parse(String(raw || "{}"))
      running = state.running === true
      loggedIn = state.loggedIn === true
      playing = state.playing === true
      title = String(state.title || "")
      artist = String(state.artist || "")
      artUrl = String(state.artUrl || "")
      duration = Number(state.duration || 0)
      position = Number(state.position || 0)
      if (state.error) lastError = String(state.error)
    } catch (error) {
      lastError = "Could not read SoundCloud status"
    }
  }

  visible: true
  implicitWidth: row.implicitWidth + Style.space(14)
  implicitHeight: barSize

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: ""
      color: root.running ? root.bar.barForeground : root.dim
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: !root.bar.vertical && root.title !== ""
      width: Math.min(Style.space(180), implicitWidth)
      textFormat: Text.PlainText
      text: root.label
      color: root.bar.barForeground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
      elide: Text.ElideRight
    }
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton

    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) root.runAction(root.running ? "play-pause" : "launch")
      else if (mouse.button === Qt.RightButton && root.running) root.runAction("next")
      else root.popupOpen = !root.popupOpen
    }
    onWheel: function(wheel) {
      if (!root.running) return
      root.runAction(wheel.angleDelta.y > 0 ? "previous" : "next")
    }
    onEntered: root.bar.showTooltip(root, root.label)
    onExited: root.bar.hideTooltip(root)
  }

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(340))
    contentHeight: popup.fittedContentHeight(content.implicitHeight)

    Column {
      id: content
      anchors.fill: parent
      spacing: Style.space(12)

      Row {
        width: parent.width
        spacing: Style.space(10)

        BorderSurface {
          width: Style.space(68)
          height: Style.space(68)
          radius: Style.spacing.labelGap
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

          Image {
            anchors.fill: parent
            anchors.margins: Style.space(2)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            source: root.artUrl
            visible: source !== ""
          }

          Text {
            anchors.centerIn: parent
            visible: root.artUrl === ""
            text: ""
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.displayLarge
          }
        }

        Column {
          width: parent.width - Style.space(78)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(3)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.title || (root.loggedIn ? "Nothing playing" : "SoundCloud")
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.artist || (!root.running ? "Backend is stopped" : (!root.loggedIn ? "Sign in once to continue" : "Choose Likes or Following"))
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
        }
      }

      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(7)
        visible: root.running && root.loggedIn

        Button {
          iconText: "󰒮"
          foreground: root.bar.foreground
          enabled: !root.actionBusy
          opacity: enabled ? 1 : 0.4
          onClicked: root.runAction("previous")
        }
        Button {
          iconText: root.playIcon
          foreground: root.bar.foreground
          iconSize: Style.font.iconLarge
          enabled: !root.actionBusy && root.title !== ""
          opacity: enabled ? 1 : 0.4
          onClicked: root.runAction("play-pause")
        }
        Button {
          iconText: "󰒭"
          foreground: root.bar.foreground
          enabled: !root.actionBusy
          opacity: enabled ? 1 : 0.4
          onClicked: root.runAction("next")
        }
      }

      PanelSeparator {
        visible: root.running && root.loggedIn
        foreground: root.bar.foreground
      }

      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(8)
        visible: root.running && root.loggedIn

        Button {
          text: "Likes"
          foreground: root.bar.foreground
          enabled: !root.actionBusy
          onClicked: root.runAction("likes")
        }
        Button {
          text: "Following"
          foreground: root.bar.foreground
          enabled: !root.actionBusy
          onClicked: root.runAction("feed")
        }
      }

      Button {
        anchors.horizontalCenter: parent.horizontalCenter
        visible: !root.running || !root.loggedIn
        text: root.running ? "Show SoundCloud sign in" : "Sign in to SoundCloud"
        foreground: root.bar.foreground
        enabled: !root.actionBusy
        onClicked: root.runAction(root.running ? "show" : "launch")
      }

      Text {
        width: parent.width
        visible: root.lastError !== ""
        textFormat: Text.PlainText
        text: root.lastError
        color: root.bar.urgent
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: "Middle-click: play/pause  ·  Right-click: next  ·  Wheel: previous/next"
        color: root.dim
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
      }
    }
  }

  Timer {
    interval: 1500
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: delayedRefresh
    interval: 450
    repeat: false
    onTriggered: root.refresh()
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector {
      id: statusOutput
      waitForEnd: true
    }
    onExited: function(_exitCode) {
      root.applyStatus(statusOutput.text)
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stdout: StdioCollector {
      id: actionOutput
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: actionError
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.actionBusy = false
      if (exitCode !== 0) root.lastError = String(actionError.text || "SoundCloud action failed").trim()
      delayedRefresh.restart()
    }
  }
}
