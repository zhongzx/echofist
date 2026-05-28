# EchoFist

Cross-platform AI-assisted Continuous Wave (CW) communication software for amateur radio (HAM) enthusiasts.

## 🎯 Project Philosophy

**Geek Spirit, Text is King** - Abandon fancy graphical interfaces, return to the essence of geek culture, focus on functionality and performance.

## ✨ Core Features

- **High Robustness Blind Demodulation**: Achieve accurate "dit-dah" detection rate in high noise and signal fading environments
- **Shared Radio Access**: Access 700+ remote receivers worldwide through the KiwiSDR network
- **Fist Feature Extraction**: Capture and record the timing characteristics of the other party's transmission (fist signature)
- **Automated QSO Process**: Reduce manual copying burden, complete communication through standardized rules
- **Anthropomorphic Replay**: Generated CW signals carry specific personality/hand perturbation

## 🚀 Quick Start

### Install Dependencies
```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# Linux/macOS
source venv/bin/activate
# Windows
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Launch Application
```bash
# Basic listening mode
python -m echofist listen --server kiwi.remotehams.com:8073

# Automatic communication mode
python -m echofist auto --freq 7.025 --wpm 20

# View help
python -m echofist --help
```

## 📁 Project Structure

```
echofist/
├── echofist/              # Main package directory
│   ├── core/             # Core modules
│   ├── ai/               # AI/ML modules
│   ├── ui/               # Text interface
│   ├── data/             # Data management
│   └── utils/            # Utility functions
├── tests/                # Test directory
├── scripts/              # Tool scripts
├── data/                 # Data files
├── docs/                 # Documentation
└── examples/             # Example code
```

## 🔧 Technology Stack

- **Audio Processing**: numpy, scipy, librosa, sounddevice
- **Text Interface**: rich, click, prompt-toolkit
- **Network Communication**: websockets, aiohttp, requests
- **Data Storage**: sqlalchemy, sqlite3, pandas
- **Machine Learning**: torch, scikit-learn, onnxruntime

## 📊 Operation Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **Listening Mode** | Real-time decoding display, no automatic response | Daily scanning, learning |
| **Semi-Automatic Mode** | Automatic decoding, manual confirmation for sending | Regular communication |
| **Full Automatic Mode** | Fully automated QSO process | Contests, unattended operation |

## 🤝 Contributing Guidelines

1. Fork the project
2. Create a feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details

## 🙏 Acknowledgments

- KiwiSDR community for providing global receiver network
- Contributors of all open-source audio processing libraries
- Continuous innovation spirit of the amateur radio community
