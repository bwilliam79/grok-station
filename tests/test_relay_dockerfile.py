import os, re, unittest

R = os.path.join(os.path.dirname(__file__), "..", "relay")


class DockerfileCopiesImports(unittest.TestCase):
    def test_local_imports_are_copied(self):
        local = {f[:-3] for f in os.listdir(R) if f.endswith(".py")}
        copied = set()
        for line in open(os.path.join(R, "Dockerfile")):
            if line.startswith("COPY "):
                copied |= {p[:-3] for p in line.split()[1:-1] if p.endswith(".py")}
        todo, seen = ["relay"], set()
        while todo:
            m = todo.pop()
            if m in seen:
                continue
            seen.add(m)
            with open(os.path.join(R, m + ".py")) as f:
                src = f.read()
            for name in re.findall(r"^(?:import|from) (\w+)", src, re.M):
                if name in local:
                    todo.append(name)
        self.assertEqual(seen - copied, set())


if __name__ == "__main__":
    unittest.main()
