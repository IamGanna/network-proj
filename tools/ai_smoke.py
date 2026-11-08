"""Opt-in paid smoke with synthetic messages only: python tools/ai_smoke.py --live."""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_history import SessionHistory
from ai_service import AI_MODEL, AI_TIMEOUT, AiFailure, AiService, load_server_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow up to two paid synthetic generations')
    args = parser.parse_args(argv)
    if not args.live:
        print('No API request made. Use --live after configuring the server .env.')
        return 0
    service = None
    try:
        config = load_server_config()
        if config is None:
            print('Configure OPENROUTER_API_KEY in network-proj/.env first.')
            return 2
        service = AiService(config)
        history = SessionHistory()
        history.record(('pm','Bob'),'Alice','Let us prepare the networking demo. I can explain TCP.')
        history.record(('pm','Bob'),'Bob','ฉันจะอธิบายกลุ่มแชท แล้วเราซ้อมพรุ่งนี้ดีไหม?')
        history.record(('pm','Bob'),'Alice','ตกลงซ้อมพรุ่งนี้ แต่ยังไม่ได้กำหนดเวลา')
        snapshot = history.snapshot(('pm','Bob'))
        print('Requested model: ' + AI_MODEL)
        for action in ('summary','draft'):
            text = service.generate(action=action, requester='Alice',snapshot=snapshot,
                                    instruction='Ask what time tomorrow.' if action=='draft' else '',
                                    deadline=time.monotonic()+AI_TIMEOUT)
            print(action + ':\n' + text)
        print('Check summary attribution/state and concise matching-language draft manually.')
        return 0
    except AiFailure as exc:
        print('AI smoke failed (' + exc.code + '): ' + exc.public_text)
        print('Check exact model alias/provider eligibility in OpenRouter. No fallback or retry was made.')
        return 1
    finally:
        if service is not None:
            service.close()


if __name__=='__main__':
    raise SystemExit(main())
