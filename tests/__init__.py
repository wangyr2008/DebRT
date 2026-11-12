"""DebRT 行为测试包。

跑法（仓库根目录）：
    python3 -m unittest discover -s tests -t .
或单个文件：
    python3 -m unittest tests.test_netgen_artifacts -v

设计约束：
  · 不需要 root、不读写 /etc、不依赖真机
  · 断言「产物内容」与「CLI 输出」，不断言源码文本
"""
import os
import sys

# 让测试文件可以直接 `import support`（无论以包还是脚本方式运行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
