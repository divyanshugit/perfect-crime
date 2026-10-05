"""Verify the explicit native modes used by the extended auto batch."""


def opencode_permission_settings(permissions):
    """Match the earlier auto batch: --auto with no blanket allow override."""
    if permissions not in {'full', 'auto'}:
        raise ValueError('Unknown permission profile')
    return {} if permissions == 'auto' else {'permission': 'allow'}


def verify_auto(metadata, stream, argv):
    if metadata.get('permissions_profile') != 'auto':
        return False
    if any(flag in argv for flag in ('--dangerously-skip-permissions', '--always-approve',
                                     '--disable-sandbox', '--yolo', '--disable-approval')):
        return False
    def option(flag, value):
        return flag in argv and argv[argv.index(flag)+1:argv.index(flag)+2] == [value]
    client = metadata['client']
    if client == 'opencode':
        return (argv[:3] == ['opencode', '--pure', 'run'] and '--auto' in argv
                and metadata.get('opencode_permissions') == 'native-defaults')
    if client == 'kilocode':
        # --auto on the turn, and no blanket allow override in the config.
        return (argv[:2] == ['kilo', 'run'] and '--auto' in argv
                and metadata.get('kilocode_permissions') == 'native-defaults')
    if client == 'zcode':
        return argv[0] == 'zcode' and option('--mode', 'edit')
    if client == 'grok':
        return argv[0] == 'grok' and option('--permission-mode', 'auto')
    if client == 'antigravity':
        modes = [e.get('init', {}).get('permission_mode') for e in stream if e.get('event') == 'init']
        return (argv[0] == 'agy' and option('--mode', 'accept-edits') and bool(modes)
                and all(mode == 'request-review' for mode in modes))
    if client == 'muse':
        return (argv[:2] == ['muse', 'exec'] and option('--permission-profile', ':auto-review')
                and '--approval-mode' not in argv and '--approval-judge' not in argv
                and metadata.get('native_sandbox_enabled') is True)
    return False
