# deploy/hooks — the VM's git hooks (source of truth)

Live copies: `~/smart_study_agent.git/hooks/` on the VM. After changing these,
reinstall with:

    scp -i ~/.ssh/smart_study.key deploy/hooks/pre-receive deploy/hooks/post-receive ubuntu@<your-server>:smart_study_agent.git/hooks/

- `pre-receive` — gate: runs `tests/test_safety_critical.py` on the pushed commit; failure rejects the push.
- `post-receive` — checks out `main` into the live tree, smoke-imports, restarts the dashboard and the WhatsApp worker (the Telegram bot is gone since 10/11/2026) (the bridge only when whatsapp/bridge.py changed).

## Sandboxed gate (prepared 07/11, needs the owner on the server)

Today `pre-receive` runs the pushed tests as ubuntu, who has passwordless sudo and reads `.env`. A commit that passes
through it runs its own code with all of that before anything rejects it. `pre-receive.sandboxed` plus `ssa-gate-run`
run the same tests as a user with no sudo, inside bwrap: no /home, no network, an empty environment. To switch:

    sudo useradd --system --no-create-home --shell /usr/sbin/nologin ssa-gate
    sudo mkdir -p /opt/ssa-gate
    sudo cp -a ~/smart_study_agent/venv /opt/ssa-gate/venv     # /home/ubuntu is 750, so the gate gets its own copy
    sudo chown -R root:root /opt/ssa-gate
    sudo chmod -R go-w /opt/ssa-gate
    scp -i ~/.ssh/smart_study.key deploy/hooks/ssa-gate-run deploy/hooks/pre-receive.sandboxed ubuntu@<your-server>:/tmp/
    sudo install -o root -g root -m 755 /tmp/ssa-gate-run /usr/local/bin/ssa-gate-run
    echo 'ubuntu ALL=(ssa-gate) NOPASSWD: /usr/local/bin/ssa-gate-run' | sudo tee /etc/sudoers.d/ssa-gate
    sudo chmod 440 /etc/sudoers.d/ssa-gate
    sudo visudo -c
    cp ~/smart_study_agent.git/hooks/pre-receive ~/smart_study_agent.git/hooks/pre-receive.ubuntu    # the way back
    install -m 755 /tmp/pre-receive.sandboxed ~/smart_study_agent.git/hooks/pre-receive

11/11/2026: the server steps above are done (ssa-gate, /opt/ssa-gate/venv, runner, sudoers) and the sandboxed
hook sits next to the live one as `hooks/pre-receive.sandboxed`; it is NOT active yet. On main before the outbox
fix of 11/11 two tests failed inside bwrap (tmpfs /tmp: two sends in one millisecond sorted at random). Once that
fix is deployed, the last two lines above switch it on.

Check it before relying on it: an empty commit pushed from the PC should print "safety tests passed". Refresh
`/opt/ssa-gate/venv` (the same copy steps) whenever requirements change, or the gate tests with old packages.
