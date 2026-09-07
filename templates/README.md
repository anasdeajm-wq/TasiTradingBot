# قوالب الاستيراد

## shariah_template.csv
قالب استيراد التصنيف الشرعي. عمود `status` يقبل: `نقي` / `مختلط` / `محرم`
(أو `PURE` / `MIXED` / `NON_COMPLIANT`). عمود `purification` اختياري.

المصدر الرسمي لمعيار العصيمي هو **مركز المقاصد** بإشراف د. محمد بن سعود
العصيمي. حمّل آخر إصدار من المركز مباشرة، وحوّله إلى هذه الصيغة، ثم:

```bash
python -m tasi.cli shariah-import ملفك.csv \
    --source "مركز المقاصد - العصيمي" \
    --as-of 2026-07-01 \
    --purification-col purification
```

التصنيف يتغيّر كل ربع سنة. كرّر الاستيراد بتاريخ سريان جديد عند كل إصدار،
والنظام يحتفظ بالتاريخ كاملاً ويستخدم الأحدث تلقائياً.

## companies.csv
قائمة الشركات: `symbol,name_ar,name_en,sector,market`. تُستورد عبر
`python -m tasi.cli universe-import companies.csv`، أو تُملأ تلقائياً من
مزوّد البيانات عند ربطه.
