@echo off
chcp 65001 >nul
rem ============================================================================
rem  recover_assets.bat —— 封面索引恢复轮（多轮循环，跑到收敛为止）
rem
rem  为什么需要这个 bat：
rem    localize.py 一轮的时间预算（LZ_BUDGET_S）是给 CI 用的安全值。在本机想把
rem    3 万张「磁盘有图、索引没键」的孤儿一次性认领回来，需要连续跑很多轮 ——
rem    而从 AI 会话里起的后台进程（nohup / run_in_background）会被**会话回收**，
rem    实测活不过一两次工具调用。只有交给 Windows 任务计划程序（schtasks）托管，
rem    进程才真正独立、跑得完。
rem
rem  背景（血案）：run_kind() 原来只在全部跑完后一次性写索引。lz4 那轮跑到
rem    30600/32858 时进程被回收，收尾没执行 → 该轮下好的图全在磁盘上，但
rem    「键→文件」映射没入库 → 36352 张图里只有 5473 条键，端上一张也认不出。
rem    现已修复：每 1000 张 checkpoint 抢写一次索引（原子替换）+ LZ_HARD_CAP 逃生阀。
rem
rem  ★ LZ_HARD_CAP 为什么必须抬到 40000：硬上限默认取 SPEC 里的 max，
rem    而恢复期间 len(old) 偏小会让「总量已满」判断失效，反而挡住认领。
rem ============================================================================
cd /d F:\2026-10-05-13-36-40\musicdl\cloud

set PY=C:\Users\sbqqq\.workbuddy\binaries\python\envs\default\Scripts\python.exe
set LZ_WORKERS=16
set LZ_MAX_NEW=40000
set LZ_HARD_CAP=40000
set LZ_BUDGET_S=900
set CV_LIB_TOP=40000

set LOG=F:\2026-10-05-13-36-40\musicdl\cloud\tools\_recover.log

for /L %%i in (1,1,12) do (
  echo === round %%i %DATE% %TIME% === >> "%LOG%"
  "%PY%" tools\localize.py localize >> "%LOG%" 2>&1
)

echo === ALL DONE %DATE% %TIME% === >> "%LOG%"
