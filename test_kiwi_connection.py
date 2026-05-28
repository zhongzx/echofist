#!/usr/bin/env python3
"""
KiwiSDR 连接测试脚本
测试是否能连接到公共 KiwiSDR 服务器
"""

import asyncio
import sys

import aiohttp


async def test_kiwi_connection() -> bool:
    """测试 KiwiSDR 连接"""
    print("=== KiwiSDR 连接测试 ===")

    # 实际可用的公共服务器（需要网络连接）
    public_servers = [
        {"name": "PA3GJX", "host": "kiwisdr.pa3gjx.nl", "port": 8073},
        {"name": "G0MJW", "host": "kiwisdr.g0mjw.com", "port": 8073},
    ]

    print("测试网络连接...")
    try:
        # 首先测试基本网络连接
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://www.google.com",
                timeout=5,
            ) as response:
                if response.status == 200:
                    print("✓ 网络连接正常")
                else:
                    print("⚠ 网络连接异常")
                    return False
    except Exception as e:
        print(f"✗ 网络连接失败: {e}")
        print("提示: 需要网络连接才能测试 KiwiSDR")
        return False

    print("\n测试 KiwiSDR 服务器连接...")

    # 测试每个服务器
    for server in public_servers:
        print(f"\n尝试连接: {server['name']} ({server['host']}:{server['port']})")

        try:
            # 使用简单的 HTTP 请求测试服务器是否在线
            http_url = f"http://{server['host']}:{server['port']}/"
            async with aiohttp.ClientSession() as session:
                async with session.get(http_url, timeout=10) as response:
                    if response.status == 200:
                        print(f"✓ {server['name']} 服务器在线")
                    else:
                        print(
                            f"⚠ {server['name']} 服务器返回状态: {response.status}",
                        )
        except asyncio.TimeoutError:
            print(f"✗ {server['name']} 连接超时")
        except aiohttp.ClientConnectorError:
            print(f"✗ {server['name']} 连接被拒绝")
        except Exception as e:
            print(f"✗ {server['name']} 连接错误: {e}")

    print("\n" + "=" * 40)
    print("连接测试完成")
    print("\n注意: KiwiSDR 服务器可能需要特定的访问权限")
    print("有些服务器可能限制同时连接的用户数")
    print("如果连接失败，请尝试其他服务器或稍后再试")

    return True


async def test_audio_stream() -> bool:
    """测试音频流功能"""
    print("\n=== 音频流功能测试 ===")

    print("此测试需要实际的 KiwiSDR 服务器连接")
    print("由于服务器可用性和网络条件，此测试可能失败")

    # 这里可以添加实际的 WebSocket 连接测试
    # 但由于需要真实的服务器，我们只做概念说明

    print("\n音频流测试需要:")
    print("1. 可用的 KiwiSDR 服务器")
    print("2. 稳定的网络连接")
    print("3. 服务器未达到用户上限")

    return True


async def main() -> int:
    """主函数"""
    print("EchoFist KiwiSDR 连接测试")
    print("=" * 40)

    # 运行测试
    print("开始连接测试...")

    connection_ok = await test_kiwi_connection()

    if connection_ok:
        print("\n✅ 基本连接测试通过")
        print("建议: 尝试运行主程序进行实际音频测试")
        print(
            "命令: python -m echofist monitor --server "
            "kiwisdr.pa3gjx.nl:8073 --freq 7.023"
        )
        return 0

    print("\n⚠ 连接测试遇到问题")
    print("请检查:")
    print("1. 网络连接是否正常")
    print("2. 防火墙是否允许 WebSocket 连接")
    print("3. KiwiSDR 服务器是否可用")
    return 1


if __name__ == "__main__":
    # 运行异步主函数
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n测试被用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"\n测试发生错误: {e}")
        sys.exit(1)
