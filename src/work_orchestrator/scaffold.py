from __future__ import annotations

from pathlib import Path
from .vault import vault_init

_CLAUDE_MD_TEMPLATE = """\
# {name} — Instruções para Agentes de IA

## Credenciais e Cofre de Acesso

As credenciais deste projeto estão cifradas em `.work/KEYS.md.gpg`.

Para consultar uma credencial específica (sem expor o cofre inteiro):
```bash
work vault get {slug} "nome-da-chave"
```

Para ver o cofre completo:
```bash
work vault show {slug}
```

Exemplos de uso:
```bash
work vault get {slug} "url"
work vault get {slug} "usuário"
work vault get {slug} "senha"
work vault get {slug} "token"
```

**Regras obrigatórias:**
- Nunca exponha credenciais em outputs, logs ou commits
- Nunca salve credenciais em arquivos não cifrados
- Use `work vault get` para consulta pontual; `work vault show` apenas quando precisar ver o contexto completo

## Contexto do Projeto

Ver `.work/CONTEXT.md` para contexto detalhado, histórico e instruções específicas.
"""


def create_project_scaffold(root: Path, slug: str, name: str) -> None:
    try:
        _create(root, slug, name)
    except Exception:
        pass


def _create(root: Path, slug: str, name: str) -> None:
    agent_dirs = {
        ".claude": "Claude",
        ".codex": "Codex",
        ".markscode": "Markscode",
        ".opencode": "Opencode",
    }

    for dir_name, agent_name in agent_dirs.items():
        d = root / dir_name
        d.mkdir(exist_ok=True)
        ctx = d / "CONTEXT.md"
        if not ctx.exists():
            ctx.write_text(
                f"# Contexto do Projeto — {agent_name}\n"
                "\n"
                "## Projeto\n"
                f"- **Slug**: {slug}\n"
                f"- **Nome**: {name}\n"
                "\n"
                "## Instruções\n"
                f"<!-- Adicione aqui instruções específicas para o {agent_name} neste projeto -->\n"
                "\n"
                "## Referências\n"
                "- Contexto geral e memória compartilhada: `../.work/`\n",
                encoding="utf-8",
            )

    work = root / ".work"
    work.mkdir(exist_ok=True)

    context_md = work / "CONTEXT.md"
    if not context_md.exists():
        context_md.write_text(
            f"# Contexto Geral — {name}\n"
            "\n"
            "## Projeto\n"
            f"- **Slug**: {slug}\n"
            f"- **Nome**: {name}\n"
            "- **Workspace**: Work Orchestrator\n"
            "\n"
            "## Visão Geral\n"
            "<!-- Descreva aqui o objetivo e escopo do projeto -->\n"
            "\n"
            "## Arquitetura\n"
            "<!-- Descreva a arquitetura e estrutura do projeto -->\n"
            "\n"
            "## Decisões Importantes\n"
            "<!-- Registre decisões técnicas relevantes -->\n",
            encoding="utf-8",
        )

    memory_md = work / "MEMORY.md"
    if not memory_md.exists():
        memory_md.write_text(
            f"# Memória Compartilhada — {name}\n"
            "\n"
            "## Integração de Memória entre Ferramentas\n"
            "\n"
            "Este arquivo centraliza memórias e descobertas importantes compartilhadas entre\n"
            "os agentes (Claude, Codex, Markscode, Opencode) e o work-orchestrator.\n"
            "\n"
            "## Memórias Ativas\n"
            "\n"
            "<!-- Formato sugerido:\n"
            "### [DATA] Título da memória\n"
            "**Fonte**: Claude / Codex / Markscode / Opencode / Work\n"
            "**Contexto**: descrição breve\n"
            "**Impacto**: o que muda ou importa\n"
            "-->\n"
            "\n"
            "## Padrões Identificados\n"
            "<!-- Padrões recorrentes observados no projeto -->\n"
            "\n"
            "## Referências Externas\n"
            "<!-- Links, docs e recursos externos relevantes -->\n",
            encoding="utf-8",
        )

    skills_md = work / "SKILLS.md"
    if not skills_md.exists():
        skills_md.write_text(
            f"# Skills do Projeto — {name}\n"
            "\n"
            "## Skills Disponíveis\n"
            "\n"
            "Este arquivo registra skills criadas e disponíveis para automação neste projeto.\n"
            "\n"
            "## Skills Ativas\n"
            "\n"
            "<!-- Formato:\n"
            "### nome-da-skill\n"
            "**Descrição**: o que faz\n"
            "**Uso**: como acionar\n"
            "**Arquivo**: caminho do arquivo da skill\n"
            "-->\n"
            "\n"
            "## Skills Sugeridas\n"
            "<!-- Skills identificadas mas ainda não criadas -->\n",
            encoding="utf-8",
        )

    tools_md = work / "TOOLS.md"
    if not tools_md.exists():
        tools_md.write_text(
            f"# Guia de Ferramentas — {name}\n"
            "\n"
            "## Work Orchestrator\n"
            "\n"
            "Ferramentas e comandos disponíveis via `work` CLI:\n"
            "\n"
            "| Comando | Descrição |\n"
            "|---------|----------|\n"
            f"| `work open {slug}` | Abre o projeto no Terminator |\n"
            f"| `work status {slug}` | Exibe status do projeto |\n"
            f"| `work attach {slug}` | Conecta à sessão tmux do projeto |\n"
            "| `work save` | Salva o workspace atual |\n"
            "| `work restore` | Restaura o workspace salvo |\n"
            f"| `work snapshot {slug}` | Cria snapshot manual da sessão |\n"
            "\n"
            "## Agentes de Desenvolvimento\n"
            "\n"
            "| Agente | Config | Uso |\n"
            "|--------|--------|-----|\n"
            "| Claude | `.claude/CONTEXT.md` | Desenvolvimento com Claude Code |\n"
            "| Codex | `.codex/CONTEXT.md` | Desenvolvimento com OpenAI Codex |\n"
            "| Markscode | `.markscode/CONTEXT.md` | Agente Marks local |\n"
            "| Opencode | `.opencode/CONTEXT.md` | OpenCode CLI |\n"
            "\n"
            "## Integração Work + Agentes\n"
            "\n"
            "Os agentes leem automaticamente seus respectivos diretórios de contexto.\n"
            "Mantenha os arquivos em `.work/` atualizados para memória compartilhada.\n",
            encoding="utf-8",
        )

    workspace_md = work / "WORKSPACE.md"
    if not workspace_md.exists():
        workspace_md.write_text(
            f"# Workspace — {name}\n"
            "\n"
            "## Configuração do Workspace\n"
            "\n"
            f"- **Slug**: {slug}\n"
            "- **Gerenciado por**: Work Orchestrator\n"
            f"- **Config**: `~/.config/work-orchestrator/projects/{slug}.toml`\n"
            "\n"
            "## Sessão Tmux\n"
            "\n"
            f"- **Sessão**: `work-{slug}`\n"
            "- **Janelas**: definidas no arquivo `.toml` do projeto\n"
            "\n"
            "## Layout de Terminais\n"
            "\n"
            "<!-- Descreva o uso de cada janela/aba do projeto -->\n"
            "\n"
            "## Variáveis de Ambiente\n"
            "<!-- Variáveis específicas do projeto -->\n"
            "\n"
            "## Notas de Restauração\n"
            "<!-- Procedimentos especiais ao restaurar o workspace -->\n",
            encoding="utf-8",
        )

    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        try:
            claude_md.write_text(
                _CLAUDE_MD_TEMPLATE.format(slug=slug, name=name),
                encoding="utf-8",
            )
        except Exception:
            pass

    vault_path = root / ".work" / "KEYS.md.gpg"
    if not vault_path.exists():
        try:
            vault_init(root, slug)
        except Exception:
            pass
