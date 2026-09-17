# deckkm

Forward this PC's keyboard + mouse to the Steam Deck over ssh. Works in Game
Mode and Desktop Mode (the Deck sees real virtual input devices).

## Install

```sh
export DECKKM_HOST=deck@<DECK_IP>                    # or deck@steamdeck on a tailnet (the default)
scp deckkm-sink.py $DECKKM_HOST:~/.local/bin/        # Deck side
ln -sf "$PWD/deckkm.py" ~/.local/bin/deckkm          # PC side (nix-shell shebang)
```

Requires key-based ssh to the Deck and python-evdev on the Deck (SteamOS ships it).

## Use

```sh
deckkm                        # grabs keyboard+mouse, forwards to $DECKKM_HOST
deckkm --host deck@<DECK_IP>  # explicit target
deckkm --list                 # show which devices would be grabbed
deckkm --hold 2               # Esc hold time in seconds (default 1)
```

**Get control back:** hold `Esc` for 1s. A quick Esc tap is forwarded normally.

Also releases automatically on: ssh drop, Ctrl-C/SIGTERM, any crash, or
`kill -9` (kernel drops the grab when the process dies). The Deck side releases
all held keys whenever the stream ends.

## Test

```sh
nix-shell --run 'pytest tests'                                         # unit + local loopback e2e
nix-shell --run 'DECKKM_HOST=deck@<DECK_IP> pytest tests/test_e2e.py'  # e2e against the Deck
```

e2e tests create a virtual source device via /dev/uinput; your real keyboard
and mouse are never grabbed.

## License

MIT
