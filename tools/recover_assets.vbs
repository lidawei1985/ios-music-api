' recover_assets.vbs —— 无窗口启动封面索引恢复轮
'
' 为什么用 VBS：schtasks 用「仅在用户登录时运行」跑 .bat 会闪一个 cmd 黑框、
' 并且会抢一下前台焦点。用户明确要求「后台弄不要影响我使用」。
' WScript.Shell.Run(..., 0, False) 的第二个参数 0 = 隐藏窗口，第三个 False = 不等待。
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c ""F:\2026-10-05-13-36-40\musicdl\cloud\tools\recover_assets.bat""", 0, False
