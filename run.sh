#!/bin/sh
set -e

# 激活虚拟环境（如果存在）
if [ -d "venv" ]; then
    echo "激活虚拟环境..."
    . venv/bin/activate
elif [ -d ".venv" ]; then
    echo "激活虚拟环境..."
    . .venv/bin/activate
fi

# 确定 Python 解释器
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "错误: 未找到 Python 解释器"
    exit 1
fi

echo "正在执行主程序: $PYTHON -m src.main"

# 'exec' 命令会用 python 进程替换当前的 shell 进程，这对于正确的信号处理至关重要。
exec $PYTHON -m src.main