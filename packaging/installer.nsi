; ---------------------------------------------------------------------
; 礼物答谢姬（GiftThanks）NSIS 安装脚本（Linux 上用 makensis 编译）
; 面向电脑小白：中文界面、免管理员权限、桌面快捷方式、卸载程序
; ---------------------------------------------------------------------
Unicode true
!include "MUI2.nsh"

!define APP_NAME "礼物答谢姬"
!define APP_VERSION "1.0.0"
!define APP_EXE "GiftThanks.exe"
!define APP_DIR "GiftThanks"

Name "${APP_NAME}"
OutFile "GiftThanks-Setup-1.0.0.exe"
InstallDir "$LOCALAPPDATA\${APP_DIR}"
RequestExecutionLevel user          ; 免管理员权限，不弹UAC
SetCompressor /SOLID lzma

; 版本信息（exe属性里可见）
VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} 安装程序"
VIAddVersionKey "FileVersion" "1.0.0.0"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"

; 界面
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_NAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

Section "主程序" SecMain
    SetOutPath "$INSTDIR"
    ; PyInstaller onedir 产物整体拷贝
    File /r "..\dist\GiftThanks\*.*"

    ; 卸载信息（控制面板可见）
    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_DIR}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_DIR}" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_DIR}" "DisplayVersion" "${APP_VERSION}"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_DIR}" "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_DIR}" "NoRepair" 1

    ; 开始菜单 + 桌面快捷方式
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe"
    CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section "Uninstall"
    ; 只删程序本体，不删用户数据（%APPDATA%\GiftThanks 里的海报和数据库保留）
    RMDir /r "$INSTDIR"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"
    Delete "$DESKTOP\${APP_NAME}.lnk"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_DIR}"
SectionEnd
