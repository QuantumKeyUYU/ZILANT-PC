# Zilant Prime Core

[![Coverage](https://img.shields.io/codecov/c/github/QuantumKeyUYU/zilant-prime-core?branch=main)](https://codecov.io/gh/QuantumKeyUYU/zilant-prime-core) [![Security](https://img.shields.io/badge/security-scan-passed-success.svg)](https://github.com/QuantumKeyUYU/zilant-prime-core/security) [![Docs](https://img.shields.io/badge/docs-available-blue.svg)](./docs/ARCH.md) ![ISO 27001](https://img.shields.io/badge/ISO27001-compliant-brightgreen.svg) [![Secrets](https://img.shields.io/badge/secrets-clean-brightgreen.svg)](./secret_leak_report.md) [![Changelog](https://img.shields.io/badge/changelog-up--to--date-blue.svg)](./CHANGELOG_AUTO.md) [![Licenses](https://img.shields.io/badge/licenses-open--source-brightgreen.svg)](./licenses_report.md) [![Compliance](https://github.com/QuantumKeyUYU/zilant-prime-core/actions/workflows/security-compliance-suite.yml/badge.svg)](https://github.com/QuantumKeyUYU/zilant-prime-core/actions/workflows/security-compliance-suite.yml) [![Threat Model](https://github.com/QuantumKeyUYU/zilant-prime-core/actions/workflows/crypto-threat-model.yml/badge.svg)](https://github.com/QuantumKeyUYU/zilant-prime-core/actions/workflows/crypto-threat-model.yml) [![Policy](https://github.com/QuantumKeyUYU/zilant-prime-core/actions/workflows/policy-enforcement.yml/badge.svg)](https://github.com/QuantumKeyUYU/zilant-prime-core/actions/workflows/policy-enforcement.yml)

Универсальная CLI и библиотека для создания безопасных контейнеров, шифрования логов, VDF-доказательств и полной DevSecOps-цепочки.

<div class="mermaid">
graph LR
  A[Акторы] -->|атакуют| B[CLI]
  B -->|шифрует| C[AEAD Core]
  C --> D[Контейнер]
  C --> E[Watchdog]
  A -->|саботаж| E
  A -->|перехват| F[Журналы]
  F -->|шифруются| G[SecureLogger]
</div>

---

## Stage 0: Secure Logging (завершено)

В этом этапе мы реализовали и протестировали компонент **SecureLogger**:

- Код записывает зашифрованные AES-GCM записи в файл и корректно их расшифровывает.
- Обработка log-injection через фильтрацию ASCII-символов и экранирование `\n`, `\r`.
- Singleton-логирование через `get_secure_logger()`.
- Полный набор тестов (100 % покрытие):
  - Проверка сериализации/десериализации (`test_secure_logging.py`).
  - Обработка некорректных строк и JSON.
  - Дополнительные поля, валидация типа ключа.
  - Сценарии tampering и пропуска битых строк.
- **SPDX-блок** добавлен в каждый файл.

### Чеклист по Secure Logging:
- [x] `SecureLogger` с AES-GCM и `read_logs()`.
- [x] Обработка отсутствия файла, некорректного base64, JSON.
- [x] Экранирование небезопасных символов.
- [x] Singleton-логгер (`get_secure_logger`).
- [x] Тесты на все ветки (`test_secure_logging*.py`).
- [x] README обновлён, добавлен статус Stage 0.

---

## Stage 1: Quantum‑Pseudo‑HSM (в работе)

В рамках первого этапа интеграции офлайн‑слоя Pseudo‑HSM были добавлены базовые
модули:

- `device_fp.py` собирает аппаратные характеристики и формирует детерминированный
  отпечаток устройства.
- `shard_secret.py` реализует XOR‑шардинг секрета.
- `counter.py` хранит монотонный счётчик в файле.
- `anti_snapshot.py` создаёт lock‑файл для обнаружения отката/снапшота.

CLI использует эти функции при старте, что закладывает фундамент для дальнейшей
работы Pseudo‑HSM.

---

## Документация

- **Threat Model**: [docs/THREATS.md](docs/THREATS.md)
- **Architecture**: [docs/ARCH.md](docs/ARCH.md)

---

## Установка

```bash
pip install zilant-prime-core

# опционально: автодополнение
source completions/zilant.bash  # bash
```

---

## Quickstart

```bash
pip install zilant-prime-core

# Шифрование файла:
zilctl pack secret.txt secret.zil

# С генерацией фейковых данных и метаданных:
zilctl pack secret.txt --fake-metadata --decoy 2 -p mypass

# Или через HashiCorp Vault (поле `password`):
export VAULT_ADDR="https://vault.example.com"
export VAULT_TOKEN="s.1a2b3c4d"
zilctl pack secret.txt --vault-path secret/data/zilant/password

# Расшифровка:
zilctl unpack secret.zil --output-dir ./out

# Honeypot-режим (выдаст приманку при ошибке пароля):
zilctl unpack secret.zil -p wrong --honeypot-test

Пример сравнения метаданных настоящего и фейкового контейнера:

```bash
zilctl uyi show-metadata secret.zil
{"magic":"ZILANT","version":1,"mode":"classic","nonce_hex":"...","orig_size":5,
"checksum_hex":"...","owner":"anonymous","timestamp":"1970-01-01T00:00:00Z","origin":"N/A"}

zilctl uyi show-metadata decoy_abcd.zil
{"magic":"ZILANT","version":1,"mode":"classic","nonce_hex":"...","orig_size":1024,
"checksum_hex":"...","owner":"anonymous","timestamp":"1970-01-01T00:00:00Z","origin":"N/A"}
```

Возможные атаки и ожидаемое поведение:

| Атака | Результат |
|-------|-----------|
| Неверный пароль в honeypot‑режиме | Создается decoy‑контейнер, запись в журнале `decoy_event` |
| Повреждение контейнера | Ошибка integrity, данные не раскрываются |
| Параллельное вскрытие | Счётчик `get_open_attempts` отражает все попытки |

## Anti-Forensics & Real-World Attacks

Decoy containers help mislead forensic analysts. Use `--decoy` and `--decoy-expire`
to create bait files that disappear after a delay. When honeypot mode is active,
a decoy is returned for invalid passwords and logged via `decoy_event`.

Potential attack vectors remain:

- Side‑channel traffic if decoys are not removed quickly.
- Correlation of access times when decoy cleanup is delayed.

### Parallel brute-force / mass attack

Track open attempts with `get_open_attempts`. Spawning many unpack processes
increments this counter, making brute‑force attempts detectable. Honeypot traps
can be triggered in parallel; each creates its own decoy container.

## Decoy lifecycle & safety FAQ

Decoy files are temporary bait containers. When created with `--decoy-expire`,
they disappear automatically after the given delay. If a decoy vanishes before
its expiration, the audit ledger records a `decoy_removed_early` event. When
cleanup occurs (either automatically or via sweep), a `decoy_purged` entry is
added.

Run `zilctl --decoy-sweep` to remove expired decoys manually. With the
`--paranoid` flag the CLI prints how many stale decoys were removed at startup.

### Shamir Secret Sharing

Разделите мастер‑ключ на части и восстановите его при необходимости:

```bash
zilctl key shard export --master-key cosign.key \
    --threshold 3 --shares 5 --output-dir shards

# храните файлы shards/share*.hex и shards/meta.json в разных безопасных местах

zilctl key shard import --input-dir shards --output-file master.key
```

Храните полученные шард‑бэкапы на отдельных офлайн‑носителях. Для восстановления
достаточно собрать ``threshold`` частей в одной директории и выполнить команду
``shard import``.

### Stream Verification

Проверить целостность большого контейнера можно без распаковки:

```bash
zilctl stream verify big.zst --key master.key
```

Проверить заголовок контейнера без распаковки можно так:

```bash
zilctl uyi verify-integrity secret.zil
zilctl uyi show-metadata secret.zil
```

Изменение хотя бы одного байта приведёт к ошибке «MAC mismatch».

### HPKE Encryption

Гибридное шифрование (Kyber768+X25519) доступно через подкоманды ``hpke``:

```bash
zilctl hpke encrypt src.bin ct.bin --pq-pub kyber.pk --x-pub x25519.pk
zilctl hpke decrypt ct.bin out.bin --pq-sk kyber.sk --x-sk x25519.sk
```

## Root Baseline

Zilant Prime Core aborts execution when root or debugging indicators are found.
The following checks are performed at import time:

- UID or GID equals zero
- Typical ``su``/Magisk paths exist
- Root filesystem is mounted writable
- SELinux enforcement disabled
- Active tracer via ``/proc/self/status``

If triggered, the process terminates with exit code ``99``.

## Breaking changes

- Root detection now executes on import and can be bypassed via `ZILANT_ALLOW_ROOT`.
- The PQ-crypto helpers were refactored; import paths may differ.

Example bypass for testing:

```bash
export ZILANT_ALLOW_ROOT=1
python -c "import zilant_prime_core"
```

`harden_linux()` prints nothing on success. You can call it explicitly:

```bash
python - <<'EOF'
import zilant_prime_core

zilant_prime_core.harden_linux()
print("hardened")
EOF
```

## Migration guide

````python
from zilant_prime_core.utils import pq_crypto

kem = pq_crypto.HybridKEM()
pk_pq, sk_pq, pk_x, sk_x = kem.generate_keypair()
ct_pq, _ss_pq, epk, _ss_x, shared = kem.encapsulate((pk_pq, pk_x))
ss = kem.decapsulate((sk_pq, sk_x), (ct_pq, epk, b""))
````

CLI registration and login via OPAQUE:

```bash
zilctl register --server https://auth.example --username alice
zilctl login --server https://auth.example --username alice
```

## Development

### Code Owners & Static Analysis

Source and tests are maintained by @QuantumKeyUYU, while documentation also lists @DocMaintainers. CI workflows fall under @DevSecOpsTeam. Pull requests run Semgrep with custom rules in `.semgrep.yml` to prevent hardcoded keys and insecure random usage.

## Security Checks

- Unified compliance suite (`security_compliance_report.md`)
- Automated crypto analysis (`crypto_threat_report.md`)
- Policy enforcement (`policy_report.md`)

## TODO Stage III

- GUI demonstration (PyQt/Web)
- Bug bounty policy updates and SECURITY.md
- Docker image with `ENTRYPOINT=python -c "import zilant_prime_core; zilant_prime_core.harden_linux()"`

^_^
