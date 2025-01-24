## How to contribute to YADT

### **Did you find a bug?**

- **Ensure the bug was not already reported** by searching on GitHub under [Issues](https://github.com/funstory-ai/yadt/issues).

Please pay special attention to:
1. Known compatibility issues with pdf2zh - see [#20](https://github.com/funstory-ai/yadt/issues/20) for details
2. Reported edge cases and limitations from downstream applications - see [#23](https://github.com/funstory-ai/yadt/issues/23) for discussion

- If you're unable to find an open issue addressing the problem, [open a new one](https://github.com/funstory-ai/yadt/issues/new?template=bug_report.md). Be sure to include a **title and clear description**, as much relevant information as possible.

### **If you wish to request changes or new features**

- Suggest your change in the [Issues](https://github.com/funstory-ai/yadt/issues/new?template=feature_request.md) section.

### **If you wish to contribute to YADT**

1. Fork this repository and clone it locally.
2. Create a new branch and make code changes on that branch. `git checkout -b feature/<feature-name>`
3. Perform development and ensure the code meets the requirements.

4. Commit your changes to your new branch.

```
git add .

git commit -m "<semantic commit message>"
```

5. Push to your repository: `git push origin feature/<feature-name>`.

6. Create a PR on GitHub and provide a detailed description.

7. Ensure all automated checks pass.

#### Basic Requirements

##### Workflow

1. Please create a fork on the main branch and develop on the forked branch.

- When submitting a Pull Request (PR), please provide detailed descriptions of the changes.

- If the PR fails automated checks (showing checks failed and red cross marks), please review the corresponding details and modify the submission to ensure the new PR passes automated checks.

2. Development and Testing

- Use the `uv run yadt` command for development and testing.

- Code formatting

3. Dependency Updates

- If new dependencies are introduced, please update the dependency list in pyproject.toml accordingly.

- It is recommended to use the `uv add` command for adding dependencies.

4. Documentation Updates

- If new command-line options are added, please update the command-line options list in README.md accordingly.

5. Commit Messages

- Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), for example: feat(translator): add openai.

6. Coding Style

- Please ensure submitted code follows basic coding style guidelines.

- Use underscore or camelCase for variable naming.