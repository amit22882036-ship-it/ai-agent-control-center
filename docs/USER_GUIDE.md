# How to use the Control Center

A plain-language guide to everyday use: adding a project, starting work, answering agents and getting
their results into your folder. For installation see the [README](../README.md); for running from
source see [Run from source](getting-started.md).

## The idea in one paragraph

You describe a piece of work. An AI agent (Codex, Claude Code or Gemini CLI, using your own sign-in)
does it in a **private copy** of your project, never in your folder directly. You can watch it, answer
its questions, redirect or stop it. When it is done you **review and apply** its changes to your
project folder. For larger work a **lead** agent can split the job and bring in **helpers**.

## 1. First run: check your AI tools

Open **Connections** (the plug icon in the sidebar).

- Each tool card says whether it is **Ready for tasks**. If a card says **Requires attention**, open it
  (**Open Codex**, **Open Claude Code**, ...) and follow the line that starts with **Next:**.
- Codex needs a one-time **billing acknowledgment**: open Codex, scroll to **Turned on and billing**,
  tick the "I understand ..." box and choose **Record acknowledgment**.
- **Allowed tools and defaults** (lower on the page) decides which tools any task may use at all.
- **Practice (Mock)** needs nothing and runs no AI. Use it to try the app.
- When the bell shows a tool update (for example a newer Codex), choose **Review update**. A newer
  Codex CLI also brings newer models.

## 2. Add a project

**Projects → Add project**, give it a name and paste the full folder path.

- A normal Git repository with at least one commit is added directly.
- An empty folder or a folder without Git: the dialog offers **Initialize Git and create first
  commit**. It shows exactly what it will write before doing anything.
- If the folder sits inside an accidental Git repository (for example your whole user folder), the
  dialog says so and offers to give the folder its own repository.
- Adding the same folder twice is refused and points you to the existing project.

## 3. Start a task

Choose **New task** (top right, or press **N**).

1. **What should be done?** Write it in plain language. Be specific about files and the expected result.
2. **Project**: choose where the work happens.
3. **Lead agent**: choose the tool. Unavailable tools say why.
4. **Model** and **Reasoning effort**: lighter models and lower effort are faster and use less of your
   plan; stronger models and higher effort suit harder work. **Provider default** uses the tool's own
   default. If that default is not offered by the installed CLI, a warning names the model the task
   will use instead.
5. **Access**: **Read only** can only look; **Workspace** can change files in the private copy (choose
   this for any task that should write code); **Full Access** also reaches outside the copy and asks
   you to confirm.
6. **Allow publishing and outside integrations**: leave off unless the task must publish or use
   outside services.
7. **Helpers** (Codex and Claude Code only): turn on **Let the lead bring in helpers** to allow splitting.
   Then choose how the plan becomes work:
   - **Lead decides**: it plans first, then splits or works alone.
   - **Lead proposes, I approve**: it plans and waits in **Needs you** until you answer.
   - **Always split when possible**: it splits whenever two or more parts can be separated.

The **Before you start** card on the right summarizes exactly what will happen. Choose **Start task**
(or **Ctrl+Enter**).

## 4. Follow the work

**Work** shows every task. Switch between **List** (**L**) and **Map** (**M**). The counters at the top
and the sidebar lists filter by what each task needs:

| You see | It means | What to do |
|---|---|---|
| **Working** | An agent is running now. | Watch it in **Output**, or redirect or stop it. |
| **Needs you** / **Waiting for you** | An agent asked a question, a plan waits for approval, or a run stopped for a reason you should see. | Open it and reply, or read the reason. |
| **Stopped: needs attention** | The run ended for a reason shown next to it (for example the tool refused the model, or a restart interrupted it). | Fix the cause, then **Start a new agent**. |
| **Waiting for helpers** | A lead is waiting for its helpers' results. It continues by itself. | Nothing; watch the helpers. |
| **Not running** | Nobody works on it now (for example you stopped it). | **Start a new agent** to continue. |
| **On hold** | Paused or blocked by something else. | See the reason in the task. |
| **Completed** / **Done** | The work finished. | Review and apply it (below). |

Select a task to open its panel: **Now** (what is happening and what you can do), **Activity**,
**Output** (the agent's full text) and **Settings** (model, effort, the agent's name and colour label).
**Open task** shows the full task page.

The bell (top right) lists what needs you and available tool updates. **Ctrl+K** finds any task,
project or page.

## 5. Answer an agent

When an agent asks something, the task moves to **Needs you** and a **Reply** button appears. Type your
answer and choose **Send reply**; the agent continues in the same session. To approve a proposed plan,
reply **approve**; describe changes to revise it, or reply **reject** to have the lead work alone.

## 6. Get the result into your project

A finished task stays in its private copy until you apply it.

1. Open the task page (**Open task**).
2. Under **Result → Applied to the project**, choose **Review and apply…**.
3. Check the list of changed files and choose **Apply**.

Applying merges the changes into your project folder. Your own edits, staged files and branch are
kept, and **nothing is committed**: review the changes in your editor or Git tool and commit them
yourself. The Control Center does not run your tests; any test results are the agent's own report.

## 7. Helpers

With helpers allowed, the lead plans, then hands separate parts to helpers, each in its own copy with
its own folder scope. Helpers use the lead's model and effort unless the lead asks for something
else. The lead reviews every helper's result: an accepted result is combined into the lead's copy;
a rejected one comes back to the lead to finish itself. When everything is in, the lead verifies the
whole task and finishes. You apply the lead's task like any other.

## 8. Stop, restart and recover

- **Stop agent** ends the current run. The task is kept and shows **Not running**; start a new agent to
  continue.
- If the Control Center closes while agents run, it stops them safely. After it starts again, an
  interrupted task shows **Stopped: needs attention · Interrupted by a backend restart**, and a lead
  that was only waiting for finished helpers continues by itself.

## 9. Organize

- **Colour labels** and **Rename**: in a task's **Settings** tab. Colours appear as sidebar filters.
- **Projects** can be reordered (drag the handle), hidden, renamed or moved to the Trash.
- **Move to Trash** moves the project **folder itself** into the Control Center Trash, with its tasks'
  history. **Restore** puts it back where it was. **Delete permanently** removes the folder, its
  private copies and its history, and asks you to type the project's name first.
- A project whose folder you deleted yourself can be removed from the Control Center the same way;
  nothing on disk is touched.

## 10. Installed app and Dev

The installed app keeps its data in `%LOCALAPPDATA%\AI Agent Control Center`. The development
launcher (`scripts\start-dev.cmd`) runs the current source code with its own separate data and shows
a **Dev** badge. Do not work on the same project in both at the same time.
