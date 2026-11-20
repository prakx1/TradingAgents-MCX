"""Optional forecasters for offline experiments. Never imported by the live desk."""
import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd


KRONOS_COMMIT = '67b630e67f6a18c9e9be918d9b4337c960db1e9a'
KRONOS_REVISION = '901c26c1332695a2a8f243eb2f37243a37bea320'
TOKENIZER_REVISION = '0e0117387f39004a9016484a186a908917e22426'


def baseline_forecasts(context, horizon):
    """Fixed controls: persistence, repeat the last 30m move, revert to MA20."""
    close = context.close
    last = float(close.iloc[-1])
    momentum = last - float(close.iloc[-horizon - 1])
    return {'persistence': last, 'momentum': last + momentum,
            'mean_reversion': float(close.tail(20).mean())}


class KronosForecaster:
    """Pinned official code/weights; seeded sampling is not calibrated probability."""
    def __init__(self, source, device='cpu', seed=20261027, samples=10):
        import torch
        source = Path(source).resolve()
        head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'], text=True)
        if head != KRONOS_COMMIT or dirty:
            raise ValueError(f'Use clean Kronos checkout at {KRONOS_COMMIT}')
        if 'model' in sys.modules:
            raise RuntimeError('Run Kronos in its dedicated research process; model module already loaded')
        sys.path.insert(0, str(source))
        module = importlib.import_module('model')
        self.torch, self.seed, self.samples = torch, seed, samples
        torch.set_num_threads(2)
        model_id, tokenizer_id = 'NeoQuasar/Kronos-small', 'NeoQuasar/Kronos-Tokenizer-base'
        model_revision = KRONOS_REVISION
        tokenizer_revision = TOKENIZER_REVISION
        tokenizer = module.KronosTokenizer.from_pretrained(tokenizer_id, revision=tokenizer_revision)
        model = module.Kronos.from_pretrained(model_id, revision=model_revision)
        model.eval(); tokenizer.eval()
        self.predictor = module.KronosPredictor(model, tokenizer, device=device, max_context=512)
        self.metadata = dict(model=model_id, model_revision=model_revision,
                             tokenizer=tokenizer_id, tokenizer_revision=tokenizer_revision,
                             source_commit=head, device=device, torch_version=torch.__version__,
                             seed=seed, sample_count=samples, temperature=0.6, top_p=0.9,
                             amount='OHLC mean times volume proxy; not exchange turnover',
                             source_sha256={str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in sorted((source / 'model').glob('*.py'))})

    def predict(self, context, cutoff, horizon, contract):
        # Stable per-window seed: adding earlier windows cannot change this forecast.
        key = f'{self.seed}|{contract}|{cutoff}'.encode()
        seed = int.from_bytes(hashlib.sha256(key).digest()[:4], 'big')
        self.torch.manual_seed(seed)
        np.random.seed(seed)
        x = context[['open', 'high', 'low', 'close', 'volume']].copy()
        y_ts = pd.Series(pd.date_range(cutoff, periods=horizon, freq='5min'))
        with self.torch.inference_mode():
            out = self.predictor.predict(x, pd.Series(x.index), y_ts, horizon,
                                         T=0.6, top_p=0.9, sample_count=self.samples, verbose=False)
        value = float(out.close.iloc[-1])
        if not np.isfinite(value) or value <= 0:
            raise ValueError('Kronos returned an invalid forecast')
        return value
