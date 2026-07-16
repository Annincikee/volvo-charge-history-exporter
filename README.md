# volvo-charge-history-exporter

沃尔沃中国区（Volvo Cars China）家充桩充电记录导出工具。

一个零依赖的 Python 脚本：登录你自己的账户，找到名下绑定的所有家充桩，把每个充电桩的历史充电记录导出为 CSV（可选附带原始 JSON）。只用 Python 标准库，不需要 `pip install`，也不需要向 Volvo 申请开发者凭证——网关签名所需的 App Key / App Secret 已内置。

> 非官方项目，与 Volvo Cars 无关联、未获其认可。使用前请阅读下方「免责声明」。

## 环境要求

- Python 3.10 及以上
- 一个绑定了至少一个家充桩的沃尔沃汽车中国区账户

## 用法

```bash
python3 export_charge_history.py
```

按提示输入手机号和密码，脚本会依次登录、获取账号下所有家充桩、导出每个充电桩的全部历史记录到 `charge_history.csv`。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--out` | CSV 输出路径，默认 `charge_history.csv` |
| `--json` | 可选，同时导出原始 JSON；必须同时添加 `--acknowledge-sensitive-json` |
| `--connector-id` | 只导出指定充电桩，可重复传入多个 |
| `--phone` | 也可通过 `VOLVO_PHONE` 环境变量传入 |

密码只能通过隐藏回显的交互提示或 `VOLVO_PASSWORD` 环境变量提供。脚本不接受 `--password`，以免密码进入 shell 历史和进程列表。

其余参数（`--app-key`、`--app-secret`、`--timeout`、`--retries`）用 `--help` 查看，通常不需要改动。

## 输出字段

`equipmentName`、`connectorId`、`orderNo`、`tradeNo`、`connectorName`、`startTime`、`endTime`、`chargeUseTime`、`chargeUsePower`、`chargeUsesPower`、`mainStatus`、`stopReason`、`stopReasonDetailCode`、`stopFailReason`

接口目前没有分页参数，脚本会一次性拉取每个 `connectorId` 的全部历史记录。

CSV 使用 UTF-8 with BOM 编码，Excel / WPS 打开中文不会乱码。

## 免责声明

脚本内置的 App Key / App Secret **不是分配给本项目的开发者凭证**，而是 Volvo 官方手机 App 自身内置、所有用户共用的网关签名固定值（通过抓包分析获得），标识的是"请求来自官方 App 客户端"，不代表你的身份——真正代表你身份的是账户密码。这对值已被 [hass-volvooncall-cn](https://github.com/idreamshen/hass-volvooncall-cn) 公开使用多年，本项目只是复用同一份公开信息。

请注意：

- 这是在调用 Volvo 未公开文档化的私有 API，大概率不符合官方 App 的用户协议条款；
- Volvo 随时可能更换或吊销签名密钥，届时脚本会报错，需要更新内置默认值或通过 `--app-key`/`--app-secret` 覆盖；
- 请仅用于导出**你自己账户**下的数据；
- 使用前请自行确认符合当地法律法规及 Volvo 服务条款，后果由使用者自行承担。

## 安全提示

- 不要把密码提交到版本库、写进公开文档、留在 shell 历史或日志里；
- 脚本不会把 token 或凭证写入磁盘，只写出你指定的 CSV / JSON；
- 终端输出不会显示完整充电桩名称或 `connectorId`，HTTP 错误响应正文也会被隐藏；
- CSV 已限定为文档列出的字段，但仍包含时间、电量、订单号和充电桩标识等个人数据；
- 原始 JSON 可能含有未来新增的额外字段，因此必须显式添加 `--acknowledge-sensitive-json`，并且不应直接分享。

## 常见问题

**登录时报错「网络异常，请重试」，但网络是通的？**

这通常不是本地网络问题，而是 Volvo 网关拒绝了请求（HTTP 层面成功，但接口返回 `success: false`）。直接使用未修改的脚本一般不会遇到；如果你改过请求头，请对照官方 App 的实际请求逐项检查。

## 本地验证

```bash
python3 -m unittest tests/test_export_charge_history.py -v
python3 export_charge_history.py --help
```

## 致谢与许可证

通信协议、签名算法、网关凭证均参考自 [hass-volvooncall-cn](https://github.com/idreamshen/hass-volvooncall-cn) 对 Volvo 中国区 API 的分析成果。

本项目采用 [MIT](LICENSE) 许可证。
