[HANDOFF-WRITE]
Task_Type: {code | retrieval}
Execution_End: {cc | cx}
Workflow_ID: {执行端标识}-task-{任务描述}-{YYYYMMDD}
Project_Path: {项目相对路径}
Branch_Name: {执行端前缀}/{分支描述}
Execution_Env: {如 Win11 / PowerShell / VS Code}

[Target_Files]
{相对路径/文件1.ext}
{相对路径/文件2.ext}

[Interface_Summary]
Modified_Symbols:
- {受影响的类名/函数名/接口名}

[Target_Goals]
- {功能目标1：仅描述 What 和 Scope，禁止出现 How}
- {功能目标2：明确边界与验收标准}

[Decision_Validation]
Final_Signer: DL-Gemini
Validation_Result: {passed | skipped}
Human_Validation_Required: {yes | no}