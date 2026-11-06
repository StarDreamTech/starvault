"""星匣 · StarVault — local archives, backup, migration and MCP."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys

import vault
import vault_mcp


VERSION = '0.1.0'
LEGACY = {
    'init': 'Configure backup sources and the private repository',
    'export': 'Back up conversations locally, without uploading',
    'sync': 'Back up, commit and push conversation text to the configured repository',
    'migrate': 'Estimate or copy associated attachments locally',
    'attachment-policy': 'Show or save which associated files future backups copy',
    'skills': 'Collect, list or restore Skill backups',
    'import-chatgpt': 'Import a ChatGPT export locally',
    'status': 'Show saved configuration and the last backup result',
    'install-schedule': 'Install hourly sync on macOS',
    'remove-schedule': 'Remove hourly sync on macOS',
}


def skill_path():
    source = Path(__file__).resolve().parent / 'skills/use-chat-history-vault'
    if (source / 'SKILL.md').is_file():
        return source
    try:
        dist = metadata.distribution('chat-history-vault')
        for item in dist.files or []:
            if item.as_posix().endswith('/skills/use-chat-history-vault/SKILL.md'):
                path = Path(dist.locate_file(item)).resolve()
                if path.is_file():
                    return path.parent
    except metadata.PackageNotFoundError:
        pass
    raise RuntimeError('User Skill is missing; reinstall chat-history-vault from the complete distribution.')


def execute(argv):
    # ponytail: forward existing parsers; keep one implementation of write operations.
    if argv and argv[0] in LEGACY:
        return vault.main(argv, prog='chat-history-vault')
    if argv and argv[0] in ('mcp', 'tools'):
        command, rest = argv[0], argv[1:]
        if command == 'tools':
            rest = ['--list-tools', *rest]
        return vault_mcp.main(rest, prog='chat-history-vault ' + command)

    parser = argparse.ArgumentParser(
        prog='chat-history-vault', description=__doc__,
        epilog='Query commands output UTF-8 JSON. Use COMMAND --help for options. '
               'Set CHAT_HISTORY_VAULT_CONFIG_DIR to select an independent configuration.')
    parser.add_argument('--version', action='version', version='%(prog)s ' + VERSION + ' (星匣 · StarVault)')
    sub = parser.add_subparsers(dest='command', required=True)
    for name, help_text in LEGACY.items():
        sub.add_parser(name, help=help_text)
    for name, help_text in (
        ('mcp', 'Run the stdio MCP server or generate its client configuration'),
        ('tools', 'List available MCP tools and schemas as JSON'),
        ('skill-path', 'Print the bundled user Skill folder; does not install it'),
    ):
        sub.add_parser(name, help=help_text)
    call = sub.add_parser('call', help='Call one MCP tool with JSON arguments; writes need --allow-write')
    call.add_argument('tool')
    call.add_argument('--archive', type=Path)
    call.add_argument('--allow-write', action='store_true')
    arguments = call.add_mutually_exclusive_group()
    arguments.add_argument('--arguments', help='JSON object; defaults to {}')
    arguments.add_argument('--arguments-file', type=Path, help='UTF-8 JSON file; accepts a Windows BOM')
    for name, help_text in (
        ('search', 'Search archived conversations'),
        ('read', 'Read one page of a conversation'),
        ('attachments', 'List one page of conversation attachments'),
    ):
        query = sub.add_parser(name, help=help_text)
        query.add_argument('--archive', type=Path, help='Archive root; otherwise use saved settings')
        query.add_argument('--offset', type=int, default=0)
        query.add_argument('--limit', type=int, default=12000 if name == 'read' else 20)
        if name == 'search':
            query.add_argument('query', nargs='?', default='')
            query.add_argument('--tool', choices=vault.TOOLS)
            query.add_argument('--project')
            query.add_argument('--device')
            query.add_argument('--archived', choices=('all', 'yes', 'no'), default='all')
        else:
            query.add_argument('branch')
            query.add_argument('session_id')
            if name == 'read':
                query.add_argument('--format', choices=('markdown', 'dialogue', 'manifest', 'operations'), default='markdown')
    if not argv:
        parser.print_help()
        return
    args = parser.parse_args(argv)
    if args.command == 'skill-path':
        print(skill_path())
        return
    if args.command == 'call':
        forwarded = ['--call', args.tool]
        for name in ('archive', 'arguments', 'arguments_file'):
            value = getattr(args, name)
            if value is not None:
                forwarded += ['--' + name.replace('_', '-'), str(value)]
        if args.allow_write:
            forwarded.append('--allow-write')
        return vault_mcp.main(forwarded, prog='chat-history-vault call')
    values = {key: value for key, value in vars(args).items()
              if key not in ('command', 'archive') and value is not None}
    if args.command == 'search':
        archived = values.pop('archived')
        if archived != 'all':
            values['archived'] = archived == 'yes'
    tool = {'search': 'search_conversations', 'read': 'read_conversation', 'attachments': 'list_attachments'}[args.command]
    forwarded = ['--call', tool, '--arguments', json.dumps(values)]
    if args.archive is not None:
        forwarded += ['--archive', str(args.archive)]
    return vault_mcp.main(forwarded, prog='chat-history-vault ' + args.command)


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        execute(list(sys.argv[1:] if argv is None else argv))
        return 0
    except BrokenPipeError:
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, vault_mcp.ProtocolError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
