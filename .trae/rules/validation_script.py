#!/usr/bin/env python3
"""
EchoFist 项目规则验证脚本
用于验证项目是否符合预定义的规则和约束
"""

import sys
from pathlib import Path
from typing import Any


class RuleValidator:
    """规则验证器"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.violations: list[dict[str, Any]] = []

    def validate_all(self) -> bool:
        """验证所有规则"""
        print("🔍 开始验证 EchoFist 项目规则...")

        # 1. 验证项目结构
        self.validate_project_structure()

        # 2. 验证 Python 版本
        self.validate_python_version()

        # 3. 验证代码质量工具配置
        self.validate_tool_configs()

        # 输出结果
        self.print_results()

        return len(self.violations) == 0

    def validate_project_structure(self) -> None:
        """验证项目结构"""
        required_dirs = [
            "echofist",
            "echofist/core",
            "echofist/ui",
            "echofist/utils",
        ]

        required_files = [
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "README.md",
            ".trae/rules/project_rules.md",
            ".trae/rules/boundary_constraints.md",
        ]

        for dir_path in required_dirs:
            full_path = self.project_root / dir_path
            if not full_path.exists():
                self.add_violation(
                    "项目结构",
                    f"缺少必需目录: {dir_path}",
                    "严重",
                )

        for file_path in required_files:
            full_path = self.project_root / file_path
            if not full_path.exists():
                self.add_violation(
                    "项目结构",
                    f"缺少必需文件: {file_path}",
                    "严重",
                )

    def validate_python_version(self) -> None:
        """验证 Python 版本"""
        required_version = (3, 10)
        current_version = sys.version_info[:2]

        if current_version < required_version:
            self.add_violation(
                "Python 版本",
                f"Python 版本过低: {current_version[0]}.{current_version[1]} "
                f"< {required_version[0]}.{required_version[1]}",
                "严重",
            )

    def validate_tool_configs(self) -> None:
        """验证代码质量工具配置"""
        # 检查 Black 配置
        black_config = self.project_root / "pyproject.toml"
        if black_config.exists():
            try:
                with open(black_config, encoding="utf-8") as f:
                    content = f.read()
                    if "[tool.black]" not in content:
                        self.add_violation(
                            "工具配置",
                            "pyproject.toml 中缺少 Black 配置",
                            "警告",
                        )
            except UnicodeDecodeError:
                self.add_violation(
                    "工具配置",
                    "无法读取 pyproject.toml 文件",
                    "警告",
                )

        # 检查 Ruff 配置
        ruff_config = self.project_root / "pyproject.toml"
        if ruff_config.exists():
            try:
                with open(ruff_config, encoding="utf-8") as f:
                    content = f.read()
                    if "[tool.ruff]" not in content:
                        self.add_violation(
                            "工具配置",
                            "pyproject.toml 中缺少 Ruff 配置",
                            "警告",
                        )
            except UnicodeDecodeError:
                self.add_violation(
                    "工具配置",
                    "无法读取 pyproject.toml 文件",
                    "警告",
                )

    def add_violation(self, category: str, message: str, severity: str) -> None:
        """添加规则违反记录"""
        self.violations.append(
            {"category": category, "message": message, "severity": severity}
        )

    def print_results(self) -> None:
        """输出验证结果"""
        print("\n" + "=" * 60)
        print("验证结果汇总")
        print("=" * 60)

        if not self.violations:
            print("✅ 所有规则检查通过！")
            return

        # 按严重程度分组
        severe = [v for v in self.violations if v["severity"] == "严重"]
        warnings = [v for v in self.violations if v["severity"] == "警告"]

        if severe:
            print("\n❌ 严重问题:")
            for violation in severe:
                print(f"  • {violation['category']}: {violation['message']}")

        if warnings:
            print("\n⚠️  警告:")
            for violation in warnings:
                print(f"  • {violation['category']}: {violation['message']}")

        print(
            f"\n📊 总计: {len(self.violations)} 个问题 "
            f"({len(severe)} 严重, {len(warnings)} 警告)"
        )

        if severe:
            print("\n💡 建议: 请先解决严重问题")


def main() -> None:
    """主函数"""
    # 项目根目录是当前目录的父目录的父目录
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent

    validator = RuleValidator(project_root)

    if validator.validate_all():
        print("\n🎉 项目符合所有规则要求！")
        sys.exit(0)
    else:
        print("\n🔧 请根据上述建议修复问题")
        sys.exit(1)


if __name__ == "__main__":
    main()
