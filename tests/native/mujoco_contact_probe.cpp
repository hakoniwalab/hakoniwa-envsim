#include <mujoco/mujoco.h>

#include <cstring>
#include <iostream>
#include <string>

int main(int argc, char* argv[]) {
    if (argc != 3) {
        std::cerr << "usage: mujoco_contact_probe MODEL.xml EXPECTED_GEOM_OR_NONE\n";
        return 2;
    }
    char error[1024] = {};
    mjModel* model = mj_loadXML(argv[1], nullptr, error, sizeof(error));
    if (model == nullptr) {
        std::cerr << "mj_loadXML failed: " << error << "\n";
        return 3;
    }
    mjData* data = mj_makeData(model);
    mj_forward(model, data);
    const std::string expected = argv[2];
    bool found = false;
    for (int index = 0; index < data->ncon; ++index) {
        const mjContact& contact = data->contact[index];
        const char* first = mj_id2name(model, mjOBJ_GEOM, contact.geom1);
        const char* second = mj_id2name(model, mjOBJ_GEOM, contact.geom2);
        std::cout << "contact " << (first ? first : "<unnamed>") << " "
                  << (second ? second : "<unnamed>") << "\n";
        if ((first && expected == first) || (second && expected == second)) {
            found = true;
        }
    }
    std::cout << "MuJoCo " << mj_versionString() << " ngeom=" << model->ngeom
              << " nmesh=" << model->nmesh << " ncon=" << data->ncon << "\n";
    const int contact_count = data->ncon;
    mj_deleteData(data);
    mj_deleteModel(model);
    if (expected == "NONE") {
        return found || contact_count != 0 ? 4 : 0;
    }
    if (expected == "ANY") {
        return contact_count > 0 ? 0 : 5;
    }
    return found ? 0 : 5;
}
