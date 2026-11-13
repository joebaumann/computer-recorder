# Modified for the recorder-only fork: local recording without API configuration.

import argparse
import asyncio
from crec import crec

def parse_args():
    parser = argparse.ArgumentParser(description='Record screenshots and mouse/keyboard events locally on macOS.')
    parser.add_argument('--user-name', '-u', type=str, default="anonymous", help='The user name to use')
    parser.add_argument('--debug', '-d', action='store_true', help='Enable debug mode')

    # Scroll filtering options
    parser.add_argument('--scroll-debounce', type=float, default=0.5,
                       help='Minimum time between scroll events (seconds, default: 0.5)')
    parser.add_argument('--scroll-min-distance', type=float, default=5.0,
                       help='Accumulated scroll distance between samples (input-event units, default: 5.0)')
    parser.add_argument('--scroll-max-frequency', type=int, default=10,
                       help='Maximum scroll events per second (default: 10)')
    parser.add_argument('--scroll-session-timeout', type=float, default=2.0,
                       help='Scroll session timeout (seconds, default: 2.0)')

    return parser.parse_args()

async def _main():
    args = parse_args()
    from crec.observers.screen import Screen

    print(f"User Name: {args.user_name}")
    print("Saving screenshots and keyboard/mouse events to ~/Downloads/records")
    print("Press Ctrl+C in this terminal to stop recording.")

    # Create Screen observer with scroll filtering configuration
    screen_observer = Screen(
        debug=args.debug,
        scroll_debounce_sec=args.scroll_debounce,
        scroll_min_distance=args.scroll_min_distance,
        scroll_max_frequency=args.scroll_max_frequency,
        scroll_session_timeout=args.scroll_session_timeout,
    )

    async with crec(args.user_name, screen_observer):
        await asyncio.Future()  # run forever (Ctrl-C to stop)

def main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("Recording stopped. Files are in ~/Downloads/records.")

if __name__ == '__main__':
    main()
