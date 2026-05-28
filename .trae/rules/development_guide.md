# EchoFist 开发指南

## 1. 开发环境设置

### 1.1 系统要求
- **操作系统**: Linux, macOS, Windows 10+
- **Python 版本**: 3.10+
- **内存**: 最低 2GB，推荐 4GB+
- **磁盘空间**: 最低 1GB

### 1.2 环境配置步骤

#### 步骤 1: 克隆项目
```bash
git clone <repository-url>
cd EchoFist
```

#### 步骤 2: 创建虚拟环境
```bash
# Linux/macOS
python -m venv .venv
source .venv/bin/activate

# Windows
python -m venv .venv
.venv\Scripts\activate
```

#### 步骤 3: 安装依赖
```bash
# 安装核心依赖
pip install -r requirements.txt

# 安装开发依赖
pip install -r requirements-dev.txt
```

#### 步骤 4: 安装可选 AI 依赖
```bash
# 如果需要 AI/ML 功能
pip install torch torchaudio
```

### 1.3 开发工具配置

#### Visual Studio Code 配置
```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.linting.enabled": true,
  "python.linting.flake8Enabled": true,
  "python.formatting.provider": "black",
  "python.linting.mypyEnabled": true,
  "editor.formatOnSave": true,
  "python.testing.pytestEnabled": true
}
```

#### PyCharm 配置
1. 设置项目解释器为 `.venv/bin/python`
2. 启用 Black 作为代码格式化工具
3. 配置 Ruff 作为代码检查工具
4. 启用 pytest 作为测试框架

## 2. 开发工作流程

### 2.1 代码开发流程

#### 步骤 1: 创建功能分支
```bash
git checkout -b feature/your-feature-name
```

#### 步骤 2: 编写代码
- 遵循项目编码规范
- 添加类型注解
- 编写单元测试

#### 步骤 3: 运行代码检查
```bash
# 格式化代码
black echofist/

# 运行代码检查
ruff check echofist/

# 运行类型检查
mypy echofist/
```

#### 步骤 4: 运行测试
```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_audio_processor.py

# 运行测试并生成覆盖率报告
pytest --cov=echofist --cov-report=html
```

#### 步骤 5: 提交代码
```bash
git add .
git commit -m "feat: 添加新功能描述"
```

#### 步骤 6: 创建合并请求
1. 推送分支到远程仓库
2. 创建合并请求
3. 等待代码审查
4. 根据反馈修改代码
5. 合并到主分支

### 2.2 代码审查流程

#### 审查要点
1. **代码质量**: 是否符合编码规范
2. **功能正确性**: 是否实现需求
3. **测试覆盖**: 是否有足够的测试
4. **性能影响**: 是否影响系统性能
5. **安全性**: 是否存在安全风险

#### 审查流程
1. 创建者请求审查
2. 审查者检查代码
3. 提出修改建议
4. 创建者修改代码
5. 审查者批准合并

## 3. 编码规范

### 3.1 命名规范

#### 变量和函数
```python
# 正确
def calculate_signal_strength():
    max_value = 100
    
# 错误
def CalculateSignalStrength():
    MaxValue = 100
```

#### 类名
```python
# 正确
class AudioProcessor:
    pass

# 错误
class audio_processor:
    pass
```

#### 常量
```python
# 正确
MAX_SAMPLE_RATE = 48000
DEFAULT_BUFFER_SIZE = 4096

# 错误
max_sample_rate = 48000
default_buffer_size = 4096
```

### 3.2 类型注解规范

#### 函数注解
```python
# 正确
def process_audio(audio_data: np.ndarray, sample_rate: int) -> np.ndarray:
    pass

# 错误
def process_audio(audio_data, sample_rate):
    pass
```

#### 可选参数
```python
# 正确
def configure_filter(cutoff_freq: float | None = None) -> None:
    pass

# 错误
def configure_filter(cutoff_freq=None):
    pass
```

#### 复杂类型
```python
# 正确
from typing import Dict, List

def analyze_signals(signals: List[np.ndarray]) -> Dict[str, float]:
    pass
```

### 3.3 文档字符串规范

#### 模块文档
```python
"""
音频处理器模块
用于音频信号的处理和增强
"""
```

#### 函数文档
```python
def normalize_signal(signal: np.ndarray) -> np.ndarray:
    """
    归一化音频信号
    
    Args:
        signal: 输入音频信号
        
    Returns:
        归一化后的音频信号
        
    Raises:
        ValueError: 如果输入信号为空
    """
    if len(signal) == 0:
        raise ValueError("输入信号不能为空")
    return signal / np.max(np.abs(signal))
```

#### 类文档
```python
class MorseDecoder:
    """
    摩尔斯电码解码器
    
    支持实时解码和批量解码，提供高鲁棒性的
    信号检测和解码功能。
    """
    
    def __init__(self, sample_rate: int = 12000):
        """
        初始化解码器
        
        Args:
            sample_rate: 音频采样率
        """
        self.sample_rate = sample_rate
```

## 4. 测试规范

### 4.1 测试结构

#### 测试目录结构
```
tests/
├── __init__.py
├── conftest.py
├── test_audio_processor.py
├── test_morse_decoder.py
├── test_kiwi_client.py
└── fixtures/
    └── test_audio_data.npy
```

#### 测试文件命名
- 单元测试: `test_<module_name>.py`
- 集成测试: `test_integration_<feature>.py`
- 性能测试: `test_performance_<component>.py`

### 4.2 测试编写规范

#### 单元测试示例
```python
import pytest
import numpy as np
from echofist.core.audio_processor import AudioProcessor


class TestAudioProcessor:
    """音频处理器测试"""
    
    def test_normalize_empty_signal(self):
        """测试空信号归一化"""
        processor = AudioProcessor()
        signal = np.array([])
        result = processor.normalize(signal)
        assert len(result) == 0
        
    def test_normalize_non_empty_signal(self):
        """测试非空信号归一化"""
        processor = AudioProcessor()
        signal = np.array([1.0, 2.0, 3.0])
        result = processor.normalize(signal)
        assert np.allclose(result, [1/3, 2/3, 1.0])
        
    def test_normalize_zero_signal(self):
        """测试全零信号归一化"""
        processor = AudioProcessor()
        signal = np.array([0.0, 0.0, 0.0])
        result = processor.normalize(signal)
        assert np.allclose(result, [0.0, 0.0, 0.0])
```

#### 异步测试示例
```python
import pytest
import asyncio
from echofist.core.kiwi_client import KiwiClient


class TestKiwiClient:
    """KiwiSDR 客户端测试"""
    
    @pytest.mark.asyncio
    async def test_connect_success(self):
        """测试成功连接"""
        client = KiwiClient("test-server", 8073)
        try:
            await client.connect()
            assert client.is_connected
        finally:
            await client.disconnect()
            
    @pytest.mark.asyncio
    async def test_connect_failure(self):
        """测试连接失败"""
        client = KiwiClient("invalid-server", 9999)
        with pytest.raises(ConnectionError):
            await client.connect()
```

### 4.3 测试覆盖率要求

#### 覆盖率目标
- 总体覆盖率: ≥ 90%
- 核心模块覆盖率: ≥ 95%
- 关键路径覆盖率: 100%

#### 覆盖率报告
```bash
# 生成 HTML 覆盖率报告
pytest --cov=echofist --cov-report=html

# 生成 XML 覆盖率报告（用于 CI）
pytest --cov=echofist --cov-report=xml
```

## 5. 代码质量检查

### 5.1 自动化检查

#### 预提交钩子
项目配置了 pre-commit 钩子，在提交前自动运行:
1. Black 代码格式化
2. Ruff 代码检查
3. mypy 类型检查
4. 测试运行

#### 配置 pre-commit
```bash
# 安装 pre-commit 钩子
pre-commit install

# 手动运行所有钩子
pre-commit run --all-files
```

### 5.2 手动检查清单

在提交代码前，请检查以下项目:

#### 代码质量
- [ ] 代码符合 PEP 8 规范
- [ ] 使用 Black 格式化
- [ ] 无 Ruff 检查错误
- [ ] 类型检查通过
- [ ] 无未使用的导入
- [ ] 无未使用的变量

#### 功能正确性
- [ ] 实现需求功能
- [ ] 处理边界情况
- [ ] 错误处理完善
- [ ] 性能符合要求

#### 测试覆盖
- [ ] 编写单元测试
- [ ] 测试覆盖关键路径
- [ ] 测试通过
- [ ] 覆盖率达标

#### 文档完整
- [ ] 更新代码注释
- [ ] 更新 API 文档
- [ ] 更新用户文档
- [ ] 更新变更日志

## 6. 故障排除

### 6.1 常见问题

#### 问题 1: 导入错误
**症状**: `ModuleNotFoundError: No module named 'echofist'`
**解决方案**:
```bash
# 确保在项目根目录
cd /path/to/EchoFist

# 安装项目包
pip install -e .
```

#### 问题 2: 类型检查错误
**症状**: `error: Need type annotation for variable`
**解决方案**:
```python
# 添加类型注解
data: list[str] = []

# 或者使用类型提示
from typing import List
data: List[str] = []
```

#### 问题 3: 测试失败
**症状**: `AssertionError` 或测试超时
**解决方案**:
1. 检查测试数据是否正确
2. 检查异步测试是否使用 `@pytest.mark.asyncio`
3. 检查测试环境配置

### 6.2 调试技巧

#### 使用日志
```python
import loguru

logger = loguru.logger

def complex_function():
    logger.debug("进入函数")
    # ... 代码 ...
    logger.info("处理完成")
```

#### 使用调试器
```python
import pdb

def problematic_function():
    # 设置断点
    pdb.set_trace()
    # ... 代码 ...
```

#### 性能分析
```bash
# 使用 cProfile 分析性能
python -m cProfile -o profile.stats your_script.py

# 分析结果
python -m pstats profile.stats
```

## 7. 贡献指南

### 7.1 贡献流程

1. **发现问题**: 在 GitHub Issues 中报告问题
2. **讨论方案**: 与维护者讨论解决方案
3. **实现代码**: 按照开发指南编写代码
4. **提交审查**: 创建合并请求等待审查
5. **合并发布**: 审查通过后合并发布

### 7.2 代码审查标准

#### 必须满足
- [ ] 代码符合项目规范
- [ ] 功能测试通过
- [ ] 文档更新完整
- [ ] 无已知安全风险

#### 建议满足
- [ ] 性能测试通过
- [ ] 向后兼容性保持
- [ ] 代码简洁易读

### 7.3 发布流程

1. **版本规划**: 确定发布内容和版本号
2. **代码冻结**: 停止新功能开发
3. **测试验证**: 全面测试和验证
4. **文档更新**: 更新所有相关文档
5. **打包发布**: 构建并发布包
6. **公告通知**: 发布公告和更新说明

---

## 附录

### A. 开发命令速查表

| 命令 | 说明 |
|------|------|
| `black echofist/` | 格式化代码 |
| `ruff check echofist/` | 代码检查 |
| `ruff check --fix echofist/` | 自动修复代码问题 |
| `mypy echofist/` | 类型检查 |
| `pytest` | 运行所有测试 |
| `pytest --cov=echofist` | 运行测试并检查覆盖率 |
| `pre-commit run --all-files` | 运行所有预提交检查 |

### B. 编码规范速查表

| 项目 | 规范 |
|------|------|
| 行宽 | 88 字符 |
| 缩进 | 4 空格 |
| 引号 | 单引号 |
| 导入顺序 | 标准库 → 第三方库 → 本地库 |
| 类型注解 | 所有函数必须包含 |
| 命名风格 | snake_case（变量/函数）, PascalCase（类） |

### C. 测试规范速查表

| 项目 | 规范 |
|------|------|
| 测试文件命名 | `test_<module>.py` |
| 测试类命名 | `Test<ClassName>` |
| 测试方法命名 | `test_<scenario>` |
| 异步测试 | 使用 `@pytest.mark.asyncio` |
| 测试夹具 | 使用 `conftest.py` 共享 |

---

*最后更新: 2026-05-28*
*维护者: EchoFist 开发团队*