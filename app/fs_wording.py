"""Short wording for the financial statements in Word (fs_word.py), in English and Arabic.

A starting point the client and the engagement team finish in Word: headings, the statement
lines and a few plain sentences, with [brackets] where something is still to be written.
"""

TEXT = {
    "en": {
        "subtitle": "Financial statements for the year ended {end}",
        "draft": "Draft, not audited",
        "contents": "Contents",
        "bs": "Statement of financial position at {end}",
        "is": "Statement of profit or loss for the year ended {end}",
        "cf": "Statement of cash flows for the year ended {end}",
        "notes": "Notes to the financial statements",
        "note": "Note",
        "in": "In {currency}",
        "general": "General information",
        "general_text": "[Describe the company: its legal form, where it is registered and what "
                        "it does.]",
        "basis": "Basis of preparation",
        "basis_text": "These financial statements are prepared under {framework}, on the "
                      "historical cost basis.",
        "policies": "[Add the accounting policies that apply.]",
        "account": "Account",
        "total": "Total",
        "unbalanced": "The statement of financial position for {year} doesn't balance by "
                      "{amount}. Map every account in the trial balance before relying on "
                      "these figures.",
        "cash_left": "The cash flows don't add up to the movement in cash: {amount} is left "
                     "over. Check that accounts are mapped to the same lines as last year.",
        "unmapped": "{n} accounts with a balance aren't on a line yet, so they are left out.",
    },
    "ar": {
        "subtitle": "القوائم المالية عن السنة المنتهية في {end}",
        "draft": "مسودة، غير مراجعة",
        "contents": "المحتويات",
        "bs": "قائمة المركز المالي في {end}",
        "is": "قائمة الأرباح أو الخسائر عن السنة المنتهية في {end}",
        "cf": "قائمة التدفقات النقدية عن السنة المنتهية في {end}",
        "notes": "الإيضاحات المتممة للقوائم المالية",
        "note": "إيضاح",
        "in": "بـ {currency}",
        "general": "معلومات عامة",
        "general_text": "[صف الشركة: شكلها القانوني ومكان تسجيلها ونشاطها.]",
        "basis": "أساس الإعداد",
        "basis_text": "أُعدت هذه القوائم المالية وفقًا لـ {framework}، على أساس التكلفة "
                      "التاريخية.",
        "policies": "[أضف السياسات المحاسبية المطبقة.]",
        "account": "الحساب",
        "total": "الإجمالي",
        "unbalanced": "قائمة المركز المالي لسنة {year} غير متوازنة بفرق {amount}. اربط كل "
                      "حسابات ميزان المراجعة قبل الاعتماد على هذه الأرقام.",
        "cash_left": "التدفقات النقدية لا تساوي الحركة في النقد: يتبقى {amount}. تحقق من ربط "
                     "الحسابات بالبنود نفسها كالسنة الماضية.",
        "unmapped": "{n} حسابات لها رصيد غير مربوطة ببند بعد، لذا لم تُدرج.",
    },
}

# The statements' own lines, as the app names them (statements.py, cashflow.py), in Arabic.
LABELS_AR = {
    "Cash and cash equivalents": "النقد وما في حكمه",
    "Trade and other receivables": "الذمم المدينة التجارية والأخرى",
    "Inventories": "المخزون",
    "Prepayments and other current assets": "المدفوعات المقدمة والأصول المتداولة الأخرى",
    "Property, plant and equipment": "الممتلكات والآلات والمعدات",
    "Intangible assets": "الأصول غير الملموسة",
    "Other non-current assets": "الأصول غير المتداولة الأخرى",
    "Trade and other payables": "الذمم الدائنة التجارية والأخرى",
    "Accruals and other current liabilities": "المصروفات المستحقة والالتزامات المتداولة الأخرى",
    "Current tax payable": "ضريبة الدخل المستحقة",
    "Short-term borrowings": "القروض قصيرة الأجل",
    "Long-term borrowings": "القروض طويلة الأجل",
    "Other non-current liabilities": "الالتزامات غير المتداولة الأخرى",
    "Share capital": "رأس المال",
    "Retained earnings and reserves": "الأرباح المبقاة والاحتياطيات",
    "Revenue": "الإيرادات",
    "Cost of sales": "تكلفة المبيعات",
    "Employee costs": "تكاليف الموظفين",
    "Rent and premises": "الإيجار والمباني",
    "Depreciation and amortisation": "الاستهلاك والإطفاء",
    "Administrative expenses": "المصروفات الإدارية",
    "Other operating expenses": "المصروفات التشغيلية الأخرى",
    "Other income": "إيرادات أخرى",
    "Finance costs": "تكاليف التمويل",
    "Income tax expense": "مصروف ضريبة الدخل",
    "Current assets": "الأصول المتداولة",
    "Non-current assets": "الأصول غير المتداولة",
    "Current liabilities": "الالتزامات المتداولة",
    "Non-current liabilities": "الالتزامات غير المتداولة",
    "Equity": "حقوق الملكية",
    "Operating expenses": "المصروفات التشغيلية",
    "Income tax": "ضريبة الدخل",
    "Gross profit": "مجمل الربح",
    "Operating profit": "الربح التشغيلي",
    "Profit before tax": "الربح قبل الضريبة",
    "Profit for the year": "ربح السنة",
    "Total assets": "إجمالي الأصول",
    "Total liabilities": "إجمالي الالتزامات",
    "Total equity": "إجمالي حقوق الملكية",
    "Total liabilities and equity": "إجمالي الالتزامات وحقوق الملكية",
    "Total current assets": "إجمالي الأصول المتداولة",
    "Total non-current assets": "إجمالي الأصول غير المتداولة",
    "Total current liabilities": "إجمالي الالتزامات المتداولة",
    "Total non-current liabilities": "إجمالي الالتزامات غير المتداولة",
    "Cash flows from operating activities": "التدفقات النقدية من الأنشطة التشغيلية",
    "(Increase) / decrease in trade and other receivables":
        "(الزيادة) / النقص في الذمم المدينة التجارية والأخرى",
    "(Increase) / decrease in inventories": "(الزيادة) / النقص في المخزون",
    "(Increase) / decrease in prepayments and other current assets":
        "(الزيادة) / النقص في المدفوعات المقدمة والأصول المتداولة الأخرى",
    "Increase / (decrease) in trade and other payables":
        "الزيادة / (النقص) في الذمم الدائنة التجارية والأخرى",
    "Increase / (decrease) in accruals and other current liabilities":
        "الزيادة / (النقص) في المستحقات والالتزامات المتداولة الأخرى",
    "Income tax paid": "ضريبة الدخل المدفوعة",
    "Net cash from operating activities": "صافي النقد من الأنشطة التشغيلية",
    "Cash flows from investing activities": "التدفقات النقدية من الأنشطة الاستثمارية",
    "Purchase of property, plant and equipment (net)": "شراء ممتلكات وآلات ومعدات (بالصافي)",
    "Purchase of intangible assets (net)": "شراء أصول غير ملموسة (بالصافي)",
    "Movement in other non-current assets": "الحركة في الأصول غير المتداولة الأخرى",
    "Net cash used in investing activities": "صافي النقد المستخدم في الأنشطة الاستثمارية",
    "Cash flows from financing activities": "التدفقات النقدية من الأنشطة التمويلية",
    "Proceeds from / (repayment of) borrowings": "المتحصل من / (سداد) القروض",
    "Proceeds from issue of shares": "المتحصل من إصدار الأسهم",
    "Movement in other non-current liabilities": "الحركة في الالتزامات غير المتداولة الأخرى",
    "Interest paid": "الفوائد المدفوعة",
    "Dividends paid and other movements in reserves":
        "توزيعات الأرباح المدفوعة والحركات الأخرى في الاحتياطيات",
    "Net cash from financing activities": "صافي النقد من الأنشطة التمويلية",
    "Net increase / (decrease) in cash": "صافي الزيادة / (النقص) في النقد",
    "Cash at the beginning of the year": "النقد في بداية السنة",
    "Cash at the end of the year": "النقد في نهاية السنة",}
