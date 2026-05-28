# EchoFist (回声手迹)

面向业余无线电（HAM）爱好者的跨平台 AI 辅助等幅电报（CW）通讯软件。

## 🎯 项目理念

**极客精神，文本为王** - 抛弃华丽的图形界面，回归极客本质，专注于功能与性能。

## ✨ 核心特性

- **高鲁棒性盲解调**：在高噪声和信号衰落环境下实现准确的"滴哒"检出率
- **共享电台接入**：通过 KiwiSDR 网络接入全球 700+ 远程接收机
- **手法特征提取**：捕捉并记录对方发报的时序特征（手迹画像）
- **自动化 QSO 流程**：减少人工抄收负担，通过标准化规则完成通联
- **拟人化重放**：生成的 CW 信号带有特定的人格化/手法扰动

## 🚀 快速开始

### 安装依赖
```bash
# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Linux/macOS
source venv/bin/activate
# Windows
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 启动应用
```bash
# 基本监听模式
python -m echofist listen --server kiwi.remotehams.com:8073

# 自动通联模式
python -m echofist auto --freq 7.025 --wpm 20

# 查看帮助
python -m echofist --help
```

## 📁 项目结构

```
echofist/
├── echofist/              # 主包目录
│   ├── core/             # 核心模块
│   ├── ai/               # AI/ML模块
│   ├── ui/               # 文本界面
│   ├── data/             # 数据管理
│   └── utils/            # 工具函数
├── tests/                # 测试目录
├── scripts/              # 工具脚本
├── data/                 # 数据文件
├── docs/                 # 文档
└── examples/             # 示例代码
```

## 🔧 技术栈

- **音频处理**：numpy, scipy, librosa, sounddevice
- **文本界面**：rich, click, prompt-toolkit
- **网络通信**：websockets, aiohttp, requests
- **数据存储**：sqlalchemy, sqlite3, pandas
- **机器学习**：torch, scikit-learn, onnxruntime

## 📊 操作模式

| 模式 | 描述 | 适用场景 |
|------|------|----------|
| **监听模式** | 实时解码显示，不自动应答 | 日常扫频、学习 |
| **半自动模式** | 自动解码，手动确认发送 | 常规通联 |
| **全自动模式** | 完全自动化的 QSO 流程 | 比赛、无人值守 |

## 🤝 贡献指南

1. Fork 项目
2. 创建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

## 🙏 致谢

- KiwiSDR 社区提供的全球接收机网络
- 所有开源音频处理库的贡献者
- 业余无线电社区的持续创新精神