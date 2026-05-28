# EchoFist

<div align="center">

**Cross-platform AI-assisted CW communication software for amateur radio**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

</div>

## 📚 Documentation Languages / 文档语言 / ドキュメント言語

Please select your preferred language:

- **[English](README_en.md)** - Full documentation in English
- **[中文](README_zh.md)** - 中文完整文档
- **[日本語](README_ja.md)** - 日本語完全ドキュメント

## 🎯 Project Overview

EchoFist is an AI-assisted Continuous Wave (CW) communication software designed for amateur radio enthusiasts. It combines traditional radio communication with modern AI techniques to enhance the CW experience.

### Key Features:
- **Blind Demodulation**: Robust signal detection in noisy environments
- **KiwiSDR Integration**: Access to global remote receivers
- **Fist Signature Analysis**: Capture and analyze operator timing characteristics
- **Automated QSO**: Streamlined communication process
- **Cross-Platform**: Works on Linux, macOS, and Windows

## 🚀 Quick Start

```bash
# Clone the repository
git clone https://github.com/yourusername/echofist.git
cd echofist

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Linux/macOS
source venv/bin/activate
# Windows
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the application
python -m echofist --help
```

## 🔧 Technology Stack

- **Audio Processing**: numpy, scipy, librosa, sounddevice
- **Text Interface**: rich, click
- **Network Communication**: websockets, aiohttp
- **Data Storage**: sqlalchemy, sqlite3
- **Machine Learning**: torch, scikit-learn (optional)

## 📊 Operation Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **Listening** | Real-time decoding, no auto-response | Learning, monitoring |
| **Semi-Auto** | Auto-decode, manual send confirmation | Regular QSO |
| **Full Auto** | Fully automated QSO process | Contests, unattended |

## 🤝 Contributing

Contributions are welcome! Please read our contributing guidelines in the language-specific documentation.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- KiwiSDR community for global receiver network
- Open-source audio processing libraries
- Amateur radio community innovation

---

<div align="center">

**Choose your language above to continue reading detailed documentation**

</div>
