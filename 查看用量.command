#!/bin/zsh
# 双击这个文件即可查看 WorkBuddy token 用量
cd "$(dirname "$0")" || exit 1

# 找一个可用的 python3
# 找一个可用的 python3（相对路径优先，便于别人 clone 后直接跑）
PY=""
for c in "$HOME/.local/bin/python3" \
         "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
         /opt/homebrew/bin/python3 \
         "$(command -v python3 2>/dev/null)" \
         /usr/bin/python3; do
  if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "没找到 python3，请先安装命令行工具：xcode-select --install"
  read -k 1 "?按任意键退出..."
  exit 1
fi

echo ""
echo "  WorkBuddy Token 用量查询"
echo "  ────────────────────────────────"
echo "   1) 本月（默认，直接回车）"
echo "   2) 全部历史"
echo "   3) 最近 7 天"
echo ""
printf "  请选择 [1]: "
read choice

# 注意：zsh 不会对 \${ARGS} 做分词，必须用数组
ARGS=()
case "$choice" in
  2) ARGS=(--all) ;;
  3) ARGS=(--days 7) ;;
  *) ARGS=() ;;
esac

"$PY" usage_stats.py "${ARGS[@]}" --open
STATUS=$?

echo ""
if [ $STATUS -eq 0 ]; then
  echo "  报告已在浏览器打开。"
else
  echo "  出错了：脚本执行失败（退出码 $STATUS），报告没有更新。请把上面的报错发给我。"
fi
echo ""
echo "  按任意键关闭本窗口..."
read -k 1
