# work-orchestrator

## Marks Workspace Runtime (staging)

A fundação `Marks Workspace Runtime` está disponível em staging e não inicia engines, sessões, GUI ou monitores. O runtime usa Python stdlib com SQLite WAL e persiste o controle fora do worktree em `XDG_STATE_HOME/work-orchestrator/runtime/<project_id>/control.sqlite3`, com diretórios `0700` e banco `0600`.

Comandos de staging:

```sh
./bin/work runtime status /caminho/projeto
./bin/work agent register claude /caminho/projeto
./bin/work agent list /caminho/projeto
./bin/work agent use opencode /caminho/projeto --worktree /caminho/projeto
./bin/work task create /caminho/projeto "título"
./bin/work claim acquire /caminho/projeto main src/app.py AGENT_ID
./bin/work handoff write /caminho/projeto AGENT_ID '{"summary":"estado"}'
./bin/work context compile /caminho/projeto
./bin/work event timeline /caminho/projeto
```

`agent use` apenas registra identidade e prepara handoff; nenhum executável externo é chamado. Detecção Git é somente leitura; criação de worktree/branch, commit e push não fazem parte desta fase.

Orquestrador stdlib para declarar, abrir, salvar e restaurar projetos como sessões tmux isoladas. O snapshot registra somente topologia: sessão, janelas, panes, diretórios, nomes, layouts, foco, timestamp e schema. Ambiente, histórico, scrollback, saída e comandos observados nunca são capturados.

## Requisitos

- Python 3.11 ou superior
- tmux
- Linux/Unix com `flock`
- Zenity em `/usr/bin/zenity` e Terminator 2.1.3 para os launchers gráficos

## Uso local

```sh
./bin/work init meu-projeto --root /caminho/do/projeto
./bin/work start meu-projeto
./bin/work open meu-projeto
./bin/work save meu-projeto
./bin/work status meu-projeto
./bin/work list
./bin/work stop meu-projeto
./bin/work stop meu-projeto --yes
./bin/work restore meu-projeto
./bin/work edit meu-projeto --name "Novo nome" --root "/novo/caminho" --command-policy never
./bin/work visuals migrate-defaults --dry-run
./bin/work visuals migrate-defaults --project meu-projeto --yes
```

Launchers gráficos executáveis diretamente do checkout:

```sh
./bin/work-gui-load
./bin/work-gui-new
./bin/work-gui-edit
./bin/work-gui-monitor
./bin/work-gui-save
./bin/work-gui-menu
```

`work-gui-load` lista slug, nome, estado, snapshot e caminho usando a API interna, avisa sobre runtime legado sem bloquear e abre os visuais declarados no Terminator. O layout `recommended` cria seis abas ordenadas: `Dev1 - Markscode`, `Dev2 - Opencode`, `Dev3 - Codex`, `Dev4 - Claude`, `Commands` e `Works`. Esses textos são somente nomes/rótulos e não iniciam engine, agente ou comando automaticamente. Cada visual aponta para uma janela tmux homônima e independente, com um único pane shell no root; somente `dev1-markscode` inicia com foco. O layout `minimal` permanece com uma janela e sem as seis abas. `work-gui-new` usa o mesmo fluxo multi-visual de carregamento para o layout recomendado, sem diálogo adicional. `work-gui-edit` permite alterar somente nome, raiz e política, mostra slug e sessão como somente leitura, confirma as mudanças e nunca abre o projeto.

`work-gui-monitor` seleciona projetos pela API interna e oferece status, início e parada do monitor sem abrir Terminator. O início só ocorre quando o marker informa que não há monitor ativo; o processo usa o executável absoluto do checkout, `--interval 5`, stdin nulo, sessão independente e log privado limitado. A parada usa exclusivamente `Monitor.stop()` e sua validação por pidfd; cancelamentos retornam sucesso.

## Notificações agênticas por aba

A observação agêntica é opt-in e permanece desligada em configurações existentes. Para um canário controlado, declare `agentic = true` em `[monitor]`; `notify_working = false` continua sendo o padrão e evita notificações de retomada/início de trabalho. Os limiares `idle_warning_seconds` e `idle_attention_seconds` continuam definindo inatividade. Não execute a migração nem inicie o monitor em todos os projetos de uma vez.

```toml
[monitor]
agentic = true
notify_working = false
idle_warning_seconds = 300
idle_attention_seconds = 600

[[windows]]
name = "dev2-opencode"
engine = "opencode"
```

`engine` é opcional e aceita somente `markscode`, `opencode`, `codex` ou `claude`. O layout `recommended` declara essas engines nas quatro janelas dev; `commands` e `works` permanecem sem engine. Configurações antigas sem `engine` continuam válidas.

O monitor agêntico usa somente IDs de sessão/janela/pane, índices, `pane_activity`, classe redigida do comando (`shell`, `engine`, `other` ou `unknown`) e estado de pane morto. O tmux precisa fornecer `pane_current_command` ao parser para a classificação local; esse valor bruto existe apenas durante o parsing da resposta, é descartado imediatamente e nunca integra objetos de domínio, exceptions, journal, spool, status, logs ou notificações. O fluxo não usa `capture-pane`, título, cwd, PID, argv, ambiente, prompt ou scrollback. `pane_activity` ausente permanece desconhecido e nunca é substituído pela hora atual.

As transições `shell` para processo não-shell e não-shell para `shell` após trabalho são heurísticas. Elas podem indicar `working` e `completed`, mas não inferem `waiting_user` nem nova fase. Estados semânticos confiáveis usam o protocolo explícito local:

```sh
./bin/work signal meu-projeto --window dev2-opencode --state working
./bin/work signal meu-projeto --window dev2-opencode --state waiting-user
./bin/work signal meu-projeto --window dev2-opencode --state phase-started
./bin/work signal meu-projeto --window dev2-opencode --state completed
./bin/work signal meu-projeto --window dev2-opencode --state failed
```

O comando valida projeto, sessão owned/token e janela declarada/existente. Não aceita mensagem nem fase livre. O sinal entra em spool privado `0600` dentro de diretório `0700`; append acima do limite é recusado sem rotação. O consumidor move atomicamente o lote para processamento e usa uma outbox durável, mantendo a entrega pendente quando `notify-send` falha ou quando ocorre exceção, para retry bounded sem perda intencional. A semântica é at-least-once: um crash depois de `notify-send` aceitar a notificação e antes do checkpoint durável pode causar duplicação; não há garantia exactly-once. Linhas corrompidas ou fora do schema são isoladas sem derrubar o monitor. `work monitor status <projeto>` preserva os campos anteriores e, quando `agentic = true`, acrescenta o evento mais recente do run atual por nome de janela. Título de notificação contém somente projeto, aba/visual e engine declarada; o corpo pertence a uma enumeração estática.

`work-gui-save` seleciona projetos tmux ativos/owned e publica um snapshot com timestamp. Cancelamento retorna sucesso, não abre Terminator e não salva abas nativas do Terminator. Snapshots pertencem ao projeto tmux; abas e layout nativos do Terminator ficam fora do contrato.

`work-gui-agent` seleciona um projeto configurado, engine (`markscode`, `claude`, `opencode` ou `codex`) e modo `prepare` ou `start`. O fluxo exibe root, worktree, branch, estado e conflitos como metadata-only; registra identity/session, handoff e bundle de contexto via runtime e adquire claim obrigatório. Caminhos disjuntos são permitidos, caminhos sobrepostos exigem `shared-readonly` e claims de escrita conflitantes são recusados. O modo `prepare` não inicia engine; o modo `start` exige lease do projeto e confirmação explícita final com executável, argv sanitizado, cwd e política. Só depois disso usa argv absoluto, `start_new_session`, ambiente allowlisted, stdin nulo e log privado limitado/redigido. Cancelamento libera claim/lease e não chama subprocesso de engine.

Launchers auxiliares `work-gui-handoff` e `work-gui-context` seguem o mesmo fluxo seguro para preparar identidade/handoff e bundle. Os arquivos `.desktop` em `desktop/` são templates de validação; nenhum launcher ou desktop é instalado em `HOME` durante o desenvolvimento.

`work-gui-menu` apresenta um único diálogo zenity com todas as ações (Carregar, Criar, Editar, Monitor, Salvar, Workspace, Agent) e delega diretamente para `load_main`, `new_main`, `edit_main`, `monitor_main`, `save_main`, `workspace_main` e `agent_main`, sem duplicar nenhuma lógica de negócio ou de segurança já existente nessas funções. Cancelamento no menu retorna sucesso sem despachar nenhuma ação.

`work workspace save` enumera todos os projetos configurados, salva no socket canônico apenas sessões principais ativas e owned, registra inativos separadamente e persiste workspace schema 3. Cada projeto contém `visuals[]` declarativos com `id`, `kind`, `order`, `title`, `root` e `attach_target`; schemas 1 e 2 são migrados conservadoramente durante a leitura. Visuais podem ser declarados por `[[visuals]]` no TOML ou preservados como override do workspace. `work workspace status` inclui conflitos unmanaged do socket canônico e sessões do socket legado somente como `external_unmanaged`. `work workspace restore --dry-run --visual terminator` lista `would-open` por visual; a execução visual exige `--yes`. Restore cria ou reutiliza para cada visual uma sessão tmux agrupada efêmera, seleciona nela a janela target e abre somente clientes ausentes com um processo Terminator independente isolado via `-u` (`--no-dbus`, equivalente ao antigo `--new-process` do Tilix) e um layout ConfigObj privado próprio via `-g`/`-l`. Com `--visual terminator-tabs`, todos os visuais pendentes de um mesmo projeto são agrupados em um único layout ConfigObj com `Notebook` (uma aba por visual) e uma única janela Terminator é aberta via `-u -g -l`. O grupo compartilha exatamente as janelas, panes e processos da sessão principal; não os duplica. PID, PTY, argv bruto, token, conteúdo e scrollback não são persistidos.

Um visual TOML usa `[[visuals]]` com `id` slug-local e `order` único, seguido por `[visuals.attach_target]` com a `session` exata do projeto e `window` opcional correspondente a uma janela declarada. O padrão recommended usa IDs e janelas homônimos: `dev1-markscode`, `dev2-opencode`, `dev3-codex`, `dev4-claude`, `commands` e `works`. `work open <slug> --window <nome>` revalida ownership e resolve a janela para ID tmux imediatamente antes do attach.

`work visuals migrate-defaults --dry-run` reconhece somente o shape padrão legado exato de `dev` com panes `codigo`/`shell` e `apoio` com um pane `shell`. Topologias customizadas são ignoradas. `--yes` cria backup privado `0600`, valida e publica config com fingerprint otimista e, para projeto inativo, migra snapshot legado metadata-only sem copiar processos. Em sessão ativa, o snapshot é preservado para re-save posterior pelo orquestrador. Após uma migração real, execute `work workspace save default --include-inactive` para atualizar o workspace default.

O campo opcional `title` em `[[visuals]]` define o rótulo exato da aba; configurações antigas sem ele mantêm o título derivado anterior. O valor legado interno `kind = "tilix"` permanece obrigatório por compatibilidade, embora a integração visual atual use Terminator.

Grupos visuais têm nome seguro e determinístico derivado da sessão principal e do ID visual, além de `@work-orchestrator-visual`, projeto, ID da sessão pai e prova hash do token sem expor o token. Colisões externas, ownership divergente, parent ausente ou recriado e grupos stale são recusados. Eles ficam fora do inventário de projetos, workspace/snapshots, monitor, autosave e avisos unmanaged. A política conservadora mantém um grupo owned após o último cliente sair; restores são idempotentes por projeto, visual e grupo. `stop` recusa grupos com clientes, valida ownership/staleness e remove apenas grupos owned sem clientes antes da sessão principal; matar um grupo não mata suas janelas ou processos compartilhados enquanto a principal existe.

Snapshots e workspaces persistem somente metadados de topologia e visuais. Identidades, tarefas, handoffs e eventos do runtime ficam no armazenamento separado do Marks Workspace Runtime. Nenhum desses mecanismos persiste scrollback, segredos, conteúdo de terminal, processos, engines ou histórico de shell após reboot; a restauração recria a topologia declarada, não a execução anterior.

`work-hook-detach <slug>` é um helper opt-in para hooks: usa o `work` real do checkout por caminho absoluto, não usa shell, valida socket e ownership pelo mesmo serviço de `work save` e registra somente erros redigidos, privados e limitados. O checkout não instala nem registra esse hook automaticamente.

Aliases explícitos de cwd podem ser declarados dentro de `[project.cwd_aliases]`, com chaves e valores absolutos, por exemplo `"/projetos/aplicacao" = "$HOME/projetos/aplicacao"`, após substituir `$HOME` pelo caminho absoluto correspondente no TOML. O destino deve existir e estar sob ou ser igual a `project.root`. A captura traduz somente uma origem inexistente cuja string corresponda exatamente a uma chave; não há prefix matching, expansão, traversal ou inferência. Se a origem existe, ela prevalece e não é traduzida. O snapshot guarda apenas o destino canônico, `cwd_translated` e contadores metadata-only, nunca a origem. Restore revalida exclusivamente o destino persistido e não altera o cwd de nenhum processo existente.

`open` é idempotente somente para uma sessão que pertença ao projeto. Em TTY ele revalida ownership e ID imediatamente antes de anexar. Sem TTY ele cria ou restaura, informa o resultado e não anexa. `restore` recusa qualquer sessão já ativa com o mesmo nome.

`stop` confirma antes de alterar estado. Automação sem TTY precisa fornecer `--yes`. Após confirmação, ele captura duas vezes e recusa divergências. A captura final e o `kill-session` são comandos síncronos adjacentes na mesma fila de comandos do cliente tmux; ela representa a topologia imediatamente anterior ao encerramento. O snapshot só substitui o anterior depois do kill bem-sucedido e com topologia idêntica. Falha de kill ou divergência preserva o snapshot anterior.

## Monitor da Fase 1

O monitor é opcional e roda em foreground:

```sh
./bin/work monitor start utm7
./bin/work monitor status utm7
./bin/work monitor stop utm7
./bin/work notify test
```

`[monitor]` aceita `idle_warning_seconds` (padrão 300), `idle_attention_seconds` (padrão 600) e `states`. A configuração TOML usa a versão de contrato `1`; chaves futuras incompatíveis devem rejeitar o arquivo. Eventos externos (`event_lines`/FD) são recusados; somente metadados internos validados do tmux geram eventos.

O monitor consulta apenas a sessão owned no socket canônico e usa a captura metadata-only de topologia. IDs são usados apenas para validar a leitura e não são identidade persistida do snapshot; cwd, título, foco, layout e dimensões fazem parte da assinatura quando suportados. PID, comando e atividade são ignorados para autosave. Mudanças estáveis aguardam debounce entre 0,5 e 1 segundo e publicam atomicamente sob locks projeto→sessão; divergências preservam o snapshot anterior. `[monitor].autosave` é opcional e tem padrão `true`, somente quando o monitor está ativo. Nunca há `capture-pane`, scrollback, saída, ambiente ou comando em snapshots. Abas/layouts nativos do Terminator ficam fora do contrato.

Inatividade de 5/10 minutos gera a mensagem `necessita atenção por parada longa`; conclusão/candidato estruturado gera `finalizou e precisa revisão/acompanhamento/análise`. O notifier chama `/usr/bin/notify-send` por argv, sem shell, timeout e redaction de credenciais, tokens, bearer, authorization, secret, password, passwd, passphrase, api_key, URLs autenticadas e controles, com fallback não bloqueante para stderr. `monitor stop` só sinaliza por pidfd após validar marker de início e identidade do comando; sem pidfd recusa a operação e não remove o pidfile. Windows, ntfy e Telegram ficam para fases futuras. Testes devem fornecer `HOME`, XDG e socket tmux temporários; sessões pessoais nunca são consultadas.

Também é possível instalar em ambiente virtual sem dependências de runtime:

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/work list
```

Para instalar os entry points no diretório de usuário:

```sh
cd <checkout>
python3 -m pip install --user .
```

Os templates em `desktop/` usam os nomes dos comandos no `PATH`. Para instalá-los no menu do usuário após garantir que o diretório de scripts do `pip --user` esteja no `PATH`:

```sh
cd <checkout>
mkdir -p "$HOME/.local/share/applications"
install -m644 desktop/*.desktop "$HOME/.local/share/applications/"
```

Também é possível usar os launchers diretamente de `<checkout>/bin` ou criar links portáveis em `$HOME/.local/bin`. Os templates não contêm quoting de shell.

## Configuração

As configurações ficam em `${XDG_CONFIG_HOME:-~/.config}/work-orchestrator/projects/<slug>.toml`. Variáveis XDG fornecidas precisam ser caminhos absolutos. Slugs aceitam apenas letras minúsculas ASCII, números e hífens, com 1 a 63 caracteres. Veja `examples/full.toml`.

```toml
[project]
name = "Minha aplicação"
root = "/srv/minha-aplicacao"
session = "work-minha-aplicacao"
command_policy = "prompt"

[[windows]]
name = "dev"
layout = "even-horizontal"
focus = true

[[windows.panes]]
name = "servidor"
cwd = "."
command = ["python3", "-m", "http.server", "8000"]
policy = "prompt"
focus = true

[[windows.panes]]
name = "shell"
cwd = "."
focus = false
```

Diretórios relativos são resolvidos a partir de `project.root`; a raiz relativa é resolvida a partir do diretório da configuração. Todos precisam existir. Chaves desconhecidas, focos ambíguos, nomes duplicados, controles em texto sensível e comandos que não sejam arrays são recusados.

`work edit` é não interativo e aceita somente `--name`, `--root` e `--command-policy`. `--expect-fingerprint` permite edição otimista a partir de uma leitura anterior. O editor mantém comentários, ordem, comandos e todos os bytes fora das linhas alteradas, valida a configuração completa e recusa strings TOML multilinha ou construções ambíguas. Configurações precisam ser arquivos regulares privados `0600`, sem symlink e com exatamente um hard link.

A publicação otimista serializa editores cooperativos pelo lock privado do slug e serializa publicações no diretório. A leitura privada e seu fingerprint ocorrem sob o lock do slug. Imediatamente antes da publicação, o arquivo é reaberto com `O_NOFOLLOW` e revalidado por tipo, owner, modo, contagem de links, identidade, metadados e conteúdo. O candidato é um temporário `0600` no mesmo diretório, sincronizado com `fsync`, e um único `os.replace` substitui o pathname ainda existente; depois, o diretório também é sincronizado. Assim, falhas ou crashes anteriores ao replace preservam o conteúdo antigo, e a publicação durável resulta integralmente no conteúdo antigo ou novo, sem janela intencional de pathname ausente nem backups residuais.

O lock e o fingerprint evitam conflitos entre editores cooperativos e detectam substituições ocorridas antes da revalidação que precede o replace. Eles não prometem CAS contra um writer não cooperativo do mesmo UID que ignore o lock, inclusive um processo com descritor previamente aberto; esse cenário está fora do threat model deste aplicativo local.

Políticas de comando:

- `always`: codifica o array e o entrega a um wrapper Python interno, que usa `os.execvp` sem shell e preserva cada argumento exatamente.
- `prompt`: mostra os argumentos com escape inequívoco e pergunta em terminal interativo; sem TTY, não executa.
- `never`: nunca executa.

Snapshots nunca contêm comandos. `restore` restaura somente a estrutura salva e não reexecuta comandos. Use `start` para aplicar comandos declarados conforme a política.

## Ownership e isolamento tmux

O socket canônico usa `$XDG_RUNTIME_DIR/work-orchestrator/tmux.sock` quando `XDG_RUNTIME_DIR` é absoluto, pertence ao UID efetivo, é um diretório real e não é gravável por grupo ou outros. Sem essa variável, `/run/user/<euid>` é preferido somente quando satisfaz os mesmos requisitos; caso contrário, usa `${XDG_STATE_HOME:-~/.local/state}/runtime/work-orchestrator/tmux.sock`. Sessões pessoais no servidor tmux padrão não são vistas nem alteradas, e um runtime pertencente a outro usuário nunca é escolhido.

Cada projeto recebe um token aleatório persistido em arquivo privado `0600`. Sessões criadas pelo programa carregam `@work-orchestrator-project` e `@work-orchestrator-token`. `open`, `save`, `stop`, `status` e `attach` recusam sessão sem markers, com slug diferente ou token divergente. Dois slugs não podem controlar a mesma identidade de sessão.

`WORK_TMUX_SOCKET` existe somente para testes: exige `WORK_ORCHESTRATOR_TESTING=1`, caminho absoluto e socket dentro do runtime privado do programa. Socket existente precisa pertencer ao usuário, ser realmente um socket e não conceder acesso a outros usuários.

Criação e restauração usam exclusivamente IDs retornados por `new-session`, `new-window` e `split-window` com `-P -F`. O funcionamento independe de `base-index` e `pane-base-index`.

## Estado, locks e snapshots

- Estado: `${XDG_STATE_HOME:-~/.local/state}/work-orchestrator`
- Runtime: `$XDG_RUNTIME_DIR/work-orchestrator` ou `/run/user/<euid>/work-orchestrator`
- Fallback sem runtime seguro: diretório privado `${XDG_STATE_HOME:-~/.local/state}/runtime/work-orchestrator`
- Snapshots: JSON atômico `0600`, com flush e `fsync` do arquivo e diretório
- Tokens: aleatórios e persistidos atomicamente em `ownership/`
- Locks: privados, sem symlink, um por slug e outro pelo hash de `socket+session`
- Subprocessos tmux: arrays, sem `shell=True`, com timeout para operações não interativas

Diretórios gerenciados precisam ser diretórios reais, privados e pertencentes ao usuário. Locks, tokens e snapshots recusam symlinks, tipos inesperados, owner divergente e permissões abertas.

## Contrato do snapshot

O schema atual é `2`. Índices numéricos de janelas e panes não fazem parte do schema nem são prometidos no restore, porque são configuração global do servidor tmux. A ordem dos arrays define a ordem restaurada. IDs embutidos no layout são normalizados pela ordem semântica e remapeados para os IDs novos no restore. São restaurados fielmente nomes, cwd, layout, foco e ordem de janelas/panes. Snapshots de schema anterior são recusados explicitamente.

## Status e erros

`status` valida snapshot e ownership da sessão ativa. `list` usa o mesmo contrato, mas isola falhas por projeto em um item com `error`, permitindo inspecionar os demais. Cada item válido inclui `cwd_translation` e `runtime.canonical_socket`/`runtime.legacy_fallback` com `path`, `different`, `active` e `conflict`. Uma sessão não fica degradada apenas porque todos os cwds inexistentes foram traduzidos por aliases válidos; divergências de cwds existentes continuam degradadas. Esse diagnóstico somente inspeciona metadados; não consulta conteúdo, não lê tokens, não encerra processos e não migra estado. Erros operacionais esperados são exibidos como `erro: ...` com código de saída 2, sem traceback.

## Desenvolvimento

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
python3 -m build
```

Os testes de integração criam sockets privados efêmeros e não usam o servidor tmux pessoal.

### Ordem global de locks

As operações agregadas adquirem os locks sempre nesta ordem única: `workspace -> project -> session`. Operações de projeto isoladas usam `project -> session`. O autosave mantém `project -> session` somente durante a captura e gravação do snapshot; ele libera esses locks antes de atualizar o workspace agregado. Nenhuma operação pode adquirir workspace depois de project ou session, evitando ciclos e deadlock.
