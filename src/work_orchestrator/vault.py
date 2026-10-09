from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import secrets
import subprocess
from pathlib import Path

from .errors import WorkError

_VAULT_TEMPLATE = """\
# Cofre de Acesso — {slug}
# Cifrado com AES256 simétrico via GPG + senha do cofre
# Comandos úteis:
#   Editar:               work vault edit {slug}
#   Ver descriptografado: work vault show {slug}
#   Inicializar:          work vault init {slug}

---

## Hosts SSH

# Formato:
# ### nome-do-host
# - Host: IP ou hostname
# - User: usuario
# - Port: 22
# - Key: ~/.ssh/nome-da-chave
# - Notas:

---

## Git / Repositórios

# Formato:
# ### nome-da-conta
# - Plataforma: GitHub / GitLab / Gitea
# - Conta: username
# - Token PAT: ghp_...
# - Repos: lista de repos relevantes
# - Notas:

---

## Sites — Testes e Publicação

# Formato:
# ### nome-do-site
# - URL: https://...
# - Usuário:
# - Senha:
# - Notas:

---

## APIs e Serviços de IA

# Formato:
# ### nome-do-servico
# - Base URL:
# - Chave API:
# - Uso:

---

## Variáveis de Ambiente

# Formato:
# VARIAVEL=valor

---

## Instruções para as IAs

# Como acessar credenciais deste projeto:
#
# MÉTODO 1 — Consulta pontual de uma chave (recomendado):
#   work vault get {slug} "nome-da-chave"
#   Exemplos:
#     work vault get {slug} "url"
#     work vault get {slug} "usuário"
#     work vault get {slug} "senha"
#     work vault get {slug} "token"
#
# MÉTODO 2 — Ver cofre completo:
#   work vault show {slug}
#
# MÉTODO 3 — Editar cofre:
#   work vault edit {slug}
#
# Regras:
# - Nunca logar ou expor credenciais em outputs/commits
# - Nunca salvar credenciais em arquivos não cifrados
# - O cofre está em: .work/KEYS.md.gpg
"""

_INIT_PASSWORD = "work-orchestrator-init"

_MASTER_HASH_FILENAME = "vault-master.hash"


def _master_hash_path(paths_config: Path) -> Path:
    return paths_config.parent / _MASTER_HASH_FILENAME


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )


def _save_master_hash(hash_file: Path, password: str) -> None:
    salt = secrets.token_bytes(32)
    digest = _hash_password(password, salt)
    hash_file.parent.mkdir(parents=True, exist_ok=True)
    hash_file.write_bytes(salt + digest)
    hash_file.chmod(0o600)


def _verify_master_password(hash_file: Path, password: str) -> bool:
    if not hash_file.exists():
        return False
    data = hash_file.read_bytes()
    salt, stored = data[:32], data[32:]
    candidate = _hash_password(password, salt)
    return hmac.compare_digest(candidate, stored)


def set_master_password(hash_file: Path) -> None:
    """Define ou troca a senha mestra. Pede duas vezes para confirmar.
    Oferece gravar WORK_VAULT_PASSWORD no .bashrc para uso sem TTY.
    """
    pw1 = getpass.getpass("[work vault] Nova senha mestra: ")
    if not pw1:
        raise WorkError("senha mestra não pode ser vazia")
    pw2 = getpass.getpass("[work vault] Confirme a senha mestra: ")
    if pw1 != pw2:
        raise WorkError("senhas não conferem")
    _save_master_hash(hash_file, pw1)
    print("Senha mestra definida com sucesso.")
    _offer_export_to_bashrc(pw1)


def _offer_export_to_bashrc(password: str) -> None:
    """Oferece gravar WORK_VAULT_PASSWORD no .bashrc do usuário atual (e root se aplicável)."""
    try:
        ans = input(
            "\nDeseja exportar WORK_VAULT_PASSWORD no .bashrc para uso automático sem TTY? [s/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans not in ("s", "sim", "y", "yes"):
        print("Variável não exportada. Para fazer manualmente:")
        print('  echo \'export WORK_VAULT_PASSWORD="<senha>"\' >> ~/.bashrc')
        return

    export_line = f'export WORK_VAULT_PASSWORD="{password}"'
    marker = "# work-orchestrator vault"
    profiles: list[Path] = []

    home = Path.home()
    profiles.append(home / ".bashrc")

    if os.geteuid() == 0:
        root_bashrc = Path("/root/.bashrc")
        if root_bashrc != home / ".bashrc":
            profiles.append(root_bashrc)

    for profile in profiles:
        try:
            content = profile.read_text() if profile.exists() else ""
            if "WORK_VAULT_PASSWORD" in content:
                lines = content.splitlines()
                new_lines = []
                for line in lines:
                    if "WORK_VAULT_PASSWORD" in line:
                        new_lines.append(f"{export_line}  {marker}")
                    else:
                        new_lines.append(line)
                profile.write_text("\n".join(new_lines) + "\n")
                print(f"Atualizado: {profile}")
            else:
                with open(profile, "a") as f:
                    f.write(f"\n{export_line}  {marker}\n")
                print(f"Adicionado: {profile}")
            profile.chmod(0o600 if profile.name == ".bashrc" else profile.stat().st_mode)
        except OSError as e:
            print(f"Aviso: não foi possível escrever em {profile}: {e}")


def require_master(hash_file: Path) -> str:
    """Pede a senha mestra, valida e retorna ela. Levanta WorkError se incorreta ou não definida.

    Em contextos sem TTY (automação, subprocess), a senha pode ser passada via
    variável de ambiente WORK_VAULT_PASSWORD — nunca use argumento CLI para isso.
    """
    if not hash_file.exists():
        raise WorkError(
            "senha mestra não configurada. Execute: work vault set-master"
        )
    env_password = os.environ.get("WORK_VAULT_PASSWORD", "").strip()
    if env_password:
        if not _verify_master_password(hash_file, env_password):
            raise WorkError("senha mestra incorreta (WORK_VAULT_PASSWORD)")
        return env_password
    password = getpass.getpass("[work vault] Senha mestra: ")
    if not _verify_master_password(hash_file, password):
        raise WorkError("senha mestra incorreta")
    return password


def _vault_path(root: Path) -> Path:
    return root / ".work" / "KEYS.md.gpg"


def _ask_vault_password(action: str) -> str:
    pwd = getpass.getpass(f"[work vault] Senha para {action} o cofre: ")
    if not pwd:
        raise WorkError("senha não pode ser vazia")
    return pwd


def _gpg_decrypt(vault: Path, password: str) -> bytes:
    result = subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--no-symkey-cache",
            "--pinentry-mode", "loopback",
            "--passphrase-fd", "0",
            "--trust-model", "always",
            "-d", str(vault),
        ],
        input=password.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        raise WorkError(
            f"falha ao descriptografar cofre: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def _gpg_encrypt(content: bytes, vault: Path, slug: str, password: str) -> None:
    tmp = Path(f"/tmp/markscode/vault_enc_{slug}_{os.getpid()}.md")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(content)
        result = subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--no-symkey-cache",
                "--pinentry-mode", "loopback",
                "--passphrase-fd", "0",
                "--trust-model", "always",
                "--symmetric",
                "--cipher-algo", "AES256",
                "-o", str(vault),
                str(tmp),
            ],
            input=password.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            raise WorkError(
                f"falha ao cifrar cofre: {result.stderr.decode(errors='replace').strip()}"
            )
    finally:
        if tmp.exists():
            subprocess.run(["shred", "-u", str(tmp)], capture_output=True)


def _editor_command(filepath: str) -> list[str]:
    import shutil
    if shutil.which("subl"):
        return ["subl", "--wait", filepath]
    if shutil.which("code"):
        return ["code", "--wait", filepath]
    visual = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if visual:
        return [visual, filepath]
    return ["nano", filepath]


def vault_init(root: Path, slug: str, hash_file: Path | None = None, password: str | None = None) -> None:
    vault = _vault_path(root)
    if vault.exists():
        raise WorkError(f"cofre já existe: {vault}")
    vault.parent.mkdir(parents=True, exist_ok=True)

    if hash_file is not None:
        password = require_master(hash_file)
        template = _VAULT_TEMPLATE.format(slug=slug)
    else:
        password = _INIT_PASSWORD
        template = (
            "# ATENÇÃO: Este cofre foi criado automaticamente com senha padrão.\n"
            "# Execute 'work vault init --reset {slug}' para definir sua senha pessoal.\n\n"
            + _VAULT_TEMPLATE
        ).format(slug=slug)

    content = template.encode("utf-8")
    _gpg_encrypt(content, vault, slug, password)


def vault_edit(root: Path, slug: str, hash_file: Path) -> None:
    work_dir = root / ".work"
    if not work_dir.exists():
        raise WorkError(f"diretório .work não existe: {work_dir}")
    vault = _vault_path(root)

    password = require_master(hash_file)

    if not vault.exists():
        answer = input(f"cofre não encontrado em {vault}. Deseja inicializar? [s/N] ").strip().lower()
        if answer == "s":
            content = _VAULT_TEMPLATE.format(slug=slug).encode("utf-8")
            vault.parent.mkdir(parents=True, exist_ok=True)
            _gpg_encrypt(content, vault, slug, password)
            print(f"cofre inicializado: {vault}")
        else:
            raise WorkError("cofre não inicializado")

    tmp = Path(f"/tmp/markscode/vault_{slug}_{os.getpid()}.md")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            content = _gpg_decrypt(vault, password)
        except WorkError:
            raise WorkError("senha mestra incorreta para este cofre")
        tmp.write_bytes(content)
        cmd = _editor_command(str(tmp))
        print(f"Abrindo cofre do projeto {slug}...")
        subprocess.run(cmd)
        updated = tmp.read_bytes()
        _gpg_encrypt(updated, vault, slug, password)
        print("Cofre atualizado e cifrado com sucesso.")
    finally:
        if tmp.exists():
            subprocess.run(["shred", "-u", str(tmp)], capture_output=True)


def vault_show(root: Path, slug: str, hash_file: Path) -> str:
    vault = _vault_path(root)
    if not vault.exists():
        raise WorkError(f"cofre não encontrado: {vault}")
    password = require_master(hash_file)
    try:
        return _gpg_decrypt(vault, password).decode("utf-8", errors="replace")
    except WorkError:
        raise WorkError("senha mestra incorreta para este cofre")


def vault_get(root: Path, slug: str, key: str, hash_file: Path) -> str:
    """Retorna o valor de uma chave nomeada do cofre.

    Busca no markdown por linhas no formato:
      - Key: valor
      - Chave API: valor
      KEY=valor
    onde 'key' é comparado sem distinção de maiúsculas/minúsculas e sem acento.

    Levanta WorkError se a chave não for encontrada.
    """
    import re
    import unicodedata

    def normalize(s: str) -> str:
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        return s.lower().strip()

    content = vault_show(root, slug, hash_file)
    norm_key = normalize(key)

    for line in content.splitlines():
        stripped = line.strip()
        m = re.match(r"^-\s+(.+?):\s+(.+)$", stripped)
        if m and normalize(m.group(1)) == norm_key:
            return m.group(2).strip()
        m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$", stripped)
        if m2 and normalize(m2.group(1)) == norm_key:
            return m2.group(2).strip()

    raise WorkError(f"chave '{key}' não encontrada no cofre do projeto {slug}")


def vault_delete(root: Path, slug: str, hash_file: Path) -> None:
    vault = _vault_path(root)
    if not vault.exists():
        raise WorkError(f"cofre não encontrado: {vault}")
    password = require_master(hash_file)
    try:
        _gpg_decrypt(vault, password)
    except WorkError:
        raise WorkError("senha mestra incorreta para este cofre")
    confirm = input(f"Digite o slug '{slug}' para confirmar exclusão: ").strip()
    if confirm != slug:
        raise WorkError("confirmação incorreta")
    subprocess.run(["shred", "-u", str(vault)], capture_output=True)
